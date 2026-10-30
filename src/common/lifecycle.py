"""Position lifecycle transitions confirmed by broker fills.

pending_open -> open        entry order filled (quantity = filled quantity)
pending_open -> entry_failed entry order ended without a fill, or was never placed
open -> closing              close order about to be submitted (exclusive, versioned)
closing -> closed_*          close order filled for the whole quantity
closing -> open              close order ended unfilled, or filled part of the quantity

Every write goes through save_position, so a writer holding a stale snapshot
gets PositionConflictError instead of reversing a newer transition.
"""

import logging
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get_config, get_sns_trade_topic
from .dynamo_io import PositionConflictError, record_trade, save_position
from .models import PositionStatus, SpreadPosition, TradeRecord, pnl_usd
from .notifications import publish_trade_alert
from .orders import (
    build_spread_request,
    cancel_and_refresh,
    fill_state,
    submit_or_recover,
    wait_for_terminal,
)

logger = logging.getLogger(__name__)

ET = ZoneInfo("US/Eastern")
CANCEL_WAIT_SECONDS = 10

CLOSED_STATUS_BY_REASON = {
    "stop": PositionStatus.CLOSED_STOP,
    "profit": PositionStatus.CLOSED_PROFIT,
    "eod": PositionStatus.CLOSED_EOD,
}


def _now_iso() -> str:
    return datetime.now(ET).isoformat()


def resolve_entry(
    trading_client, tables: dict, position: SpreadPosition, order, cancel_if_working: bool
) -> SpreadPosition:
    """Apply the entry order's state to a pending_open position and return the result.

    order is None when the broker has no order for the position's client order ID.
    A working order is left pending unless cancel_if_working is set.
    """
    if order is not None and cancel_if_working:
        order = cancel_and_refresh(trading_client, order, CANCEL_WAIT_SECONDS)
    if order is None:
        logger.warning(f"No entry order at the broker for {position.spread_id}")
        return _mark_entry_failed(tables, position)
    state = fill_state(order)
    if not state.terminal:
        return position
    if state.filled_qty == 0:
        logger.info(f"Entry {position.spread_id} ended {order.status} without a fill")
        return _mark_entry_failed(tables, position)
    return _confirm_entry(tables, position, state.filled_qty, state.net_credit)


def _mark_entry_failed(tables: dict, position: SpreadPosition) -> SpreadPosition:
    failed = replace(position, status=PositionStatus.ENTRY_FAILED.value, status_time=_now_iso())
    return save_position(tables["positions"], failed)


def _confirm_entry(
    tables: dict, position: SpreadPosition, filled_qty: int, credit: float
) -> SpreadPosition:
    config = get_config(position.strategy)
    opened = replace(
        position,
        status=PositionStatus.OPEN.value,
        status_time=_now_iso(),
        quantity=filled_qty,
        net_credit=credit,
        max_loss=round(position.max_loss + position.net_credit - credit, 4),
        stop_loss_price=round(credit * config.stop_loss_multiplier, 4),
        take_profit_price=round(credit * config.take_profit_pct, 4),
    )
    # The trade key is deterministic, so a repeated confirmation rewrites the same record.
    record_trade(
        tables["trades"],
        TradeRecord(
            trade_date=position.entry_time[:10],
            spread_id=position.spread_id,
            strategy=position.strategy,
            action="OPEN",
            spread_type=position.spread_type,
            underlying=position.underlying,
            quantity=filled_qty,
            net_credit=credit,
            market_regime=position.market_regime,
            vix=position.vix_at_entry,
            spy_price=position.spy_price_at_entry,
            expected_move=position.expected_move_at_entry,
        ),
    )
    saved = save_position(tables["positions"], opened)
    _notify(saved, "OPEN", credit)
    return saved


def start_close(
    trading_client,
    tables: dict,
    position: SpreadPosition,
    reason: str,
    limit_debit: float,
    wait_seconds: float,
) -> SpreadPosition | None:
    """Move an open position to closing, submit the close order and apply its result.

    Returns None if another writer changed the position first; that writer owns
    the close.
    """
    closing = replace(
        position,
        status=PositionStatus.CLOSING.value,
        status_time=_now_iso(),
        close_reason=reason,
        close_client_order_id=f"{position.spread_id}-c{position.version + 1}",
    )
    try:
        closing = save_position(tables["positions"], closing)
    except PositionConflictError:
        logger.info(f"{position.spread_id} changed before the close; skipping")
        return None
    request = build_spread_request(
        closing,
        opening=False,
        limit_price=limit_debit,
        client_order_id=closing.close_client_order_id,
    )
    order = submit_or_recover(trading_client, request)
    order = wait_for_terminal(trading_client, order, wait_seconds)
    return resolve_close(trading_client, tables, closing, order, cancel_if_working=False)


def resolve_close(
    trading_client, tables: dict, position: SpreadPosition, order, cancel_if_working: bool
) -> SpreadPosition:
    """Apply the close order's state to a closing position and return the result."""
    if order is not None and cancel_if_working:
        order = cancel_and_refresh(trading_client, order, CANCEL_WAIT_SECONDS)
    if order is None:
        logger.warning(f"No close order at the broker for {position.spread_id}")
        return _reopen(tables, position, position.quantity)
    state = fill_state(order)
    if not state.terminal:
        return position
    if state.filled_qty == 0:
        logger.warning(f"Close of {position.spread_id} ended {order.status} without a fill")
        return _reopen(tables, position, position.quantity)
    return _confirm_close(tables, position, state.filled_qty, -state.net_credit)


def _reopen(tables: dict, position: SpreadPosition, quantity: int) -> SpreadPosition:
    reopened = replace(
        position,
        status=PositionStatus.OPEN.value,
        status_time=_now_iso(),
        quantity=quantity,
        close_reason="",
        close_client_order_id="",
    )
    return save_position(tables["positions"], reopened)


def _confirm_close(
    tables: dict, position: SpreadPosition, filled_qty: int, debit: float
) -> SpreadPosition:
    realized = pnl_usd(position.net_credit, debit, filled_qty)
    record_trade(
        tables["trades"],
        TradeRecord(
            trade_date=datetime.now(ET).strftime("%Y-%m-%d"),
            spread_id=position.spread_id,
            strategy=position.strategy,
            action="CLOSE",
            spread_type=position.spread_type,
            underlying=position.underlying,
            quantity=filled_qty,
            net_credit=position.net_credit,
            close_value=debit,
            realized_pnl=realized,
            close_reason=position.close_reason,
            order_ref=position.close_client_order_id,
        ),
    )
    remaining = position.quantity - filled_qty
    if remaining > 0:
        logger.warning(f"{position.spread_id}: {filled_qty} closed, {remaining} still open")
        saved = _reopen(tables, position, remaining)
    else:
        status = CLOSED_STATUS_BY_REASON.get(position.close_reason, PositionStatus.CLOSED_MANUAL)
        closed = replace(
            position,
            status=status.value,
            status_time=_now_iso(),
            current_value=debit,
            unrealized_pnl=0.0,
        )
        saved = save_position(tables["positions"], closed)
    _notify(saved, f"CLOSE ({position.close_reason})", realized)
    logger.info(f"Closed {filled_qty} of {position.spread_id}: pnl_usd={realized:.2f}")
    return saved


def _notify(position: SpreadPosition, action: str, amount: float) -> None:
    try:
        publish_trade_alert(
            get_sns_trade_topic(),
            position.strategy,
            action,
            position.spread_id,
            position.spread_type,
            amount,
            position.underlying,
        )
    except Exception:  # noqa: BLE001 the transition is stored; the alert is best effort
        logger.warning(f"Failed to send trade alert for {position.spread_id}")
