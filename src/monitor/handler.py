"""Monitor Lambda: poll active positions, settle pending orders, check stop loss / take profit.

Runs every 5 minutes via Step Functions. It returns without broker calls when no
position is active, and without trading when the broker clock reports the
market closed. Each run:
1. settles entry and close orders that earlier runs left unconfirmed,
2. prices open positions from fresh quotes for every leg of the stored contracts,
3. starts a close on a threshold breach, or for every open position once the
   session's close deadline (derived from the broker clock) has passed.
"""

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.requests import OptionLatestQuoteRequest

from common.alpaca_clients import create_options_data_client, create_trading_client
from common.config import (
    get_config,
    get_positions_table,
    get_sns_error_topic,
    get_trades_table,
)
from common.dynamo_io import PositionConflictError, get_active_positions, save_position
from common.lifecycle import resolve_close, resolve_entry, start_close
from common.models import PositionStatus, SpreadLeg, SpreadPosition, pnl_usd
from common.notifications import publish_error_alert
from common.option_symbols import build_option_symbol
from common.orders import find_order_by_client_id
from common.session import close_deadline, session_now

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")
CLOSE_FILL_WAIT_SECONDS = 10
MAX_QUOTE_AGE = timedelta(minutes=2)
# An executor invocation lasts at most 120 s; younger pending entries are still its own.
PENDING_ENTRY_GRACE = timedelta(minutes=3)
PENDING_CLOSE_GRACE = timedelta(minutes=1)


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Args:
        event: Unused.
        context: Lambda context (unused).

    Returns:
        Dict with counts per outcome. Failures raise, so Step Functions retries.
    """
    try:
        return process_active_positions(force_close=False)
    except Exception as e:
        logger.exception("Monitor failed")
        _try_error_alert("monitor", str(e))
        raise


def process_active_positions(force_close: bool) -> dict:
    """Process every active position. force_close closes open positions regardless of P&L."""
    tables = {"positions": get_positions_table(), "trades": get_trades_table()}
    positions = get_active_positions(tables["positions"])
    if not positions:
        return {"positions_checked": 0}

    clock = create_trading_client(get_config(positions[0].strategy).secret_name).get_clock()
    if not clock.is_open:
        return {"skipped": True, "reason": "market_closed", "positions_active": len(positions)}

    outcomes: dict[str, int] = {}
    errors = []
    for position in positions:
        config = get_config(position.strategy)
        past_deadline = session_now(clock) >= close_deadline(clock, config.force_close_time)
        try:
            outcome = process_position(
                config.secret_name,
                tables,
                position,
                session_now(clock),
                force_close or past_deadline,
            )
        except Exception as e:
            logger.exception(f"Failed to process {position.spread_id}")
            errors.append(f"{position.spread_id}: {e}")
            continue
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    if errors:
        raise RuntimeError(f"{len(errors)} position(s) failed: {'; '.join(errors)}")
    return {"positions_checked": len(positions), **outcomes}


def process_position(
    secret_name: str, tables: dict, position: SpreadPosition, now: datetime, force_close: bool
) -> str:
    """Advance one position and return its outcome, such as open, closed or unpriced."""
    trading_client = create_trading_client(secret_name)
    if position.status == PositionStatus.PENDING_OPEN.value:
        if _age(position, now) < PENDING_ENTRY_GRACE:
            return "pending_open"
        order = find_order_by_client_id(trading_client, position.spread_id)
        position = resolve_entry(trading_client, tables, position, order, cancel_if_working=True)
    elif position.status == PositionStatus.CLOSING.value:
        if _age(position, now) < PENDING_CLOSE_GRACE:
            return "closing"
        order = None
        if position.close_client_order_id:
            order = find_order_by_client_id(trading_client, position.close_client_order_id)
        position = resolve_close(trading_client, tables, position, order, cancel_if_working=True)

    if position.status != PositionStatus.OPEN.value:
        return _outcome(position)
    return _evaluate_open(trading_client, secret_name, tables, position, now, force_close)


def _evaluate_open(
    trading_client,
    secret_name: str,
    tables: dict,
    position: SpreadPosition,
    now: datetime,
    force_close: bool,
) -> str:
    if date.fromisoformat(position.expiration_date) < now.date():
        message = f"{position.spread_id} expired {position.expiration_date} while open"
        logger.error(message)
        _try_error_alert("monitor/expired", f"{message}; reconcile with the broker manually")
        return "expired"

    priced = price_position(create_options_data_client(secret_name), position, now)
    if priced is None and not force_close:
        return "unpriced"
    if priced is None:
        _try_error_alert(
            "monitor/eod", f"{position.spread_id}: no valid quotes; closing at the last value"
        )
        priced = position

    reason = "eod" if force_close else _check_thresholds(priced)
    if reason is None:
        try:
            save_position(tables["positions"], priced)
        except PositionConflictError:
            return "conflict"
        return "open"

    result = start_close(
        trading_client, tables, priced, reason, priced.current_value, CLOSE_FILL_WAIT_SECONDS
    )
    return "conflict" if result is None else _outcome(result)


def _outcome(position: SpreadPosition) -> str:
    return "closed" if position.status.startswith("closed") else position.status


def _age(position: SpreadPosition, now: datetime) -> timedelta:
    if not position.status_time:
        return timedelta.max
    return now - datetime.fromisoformat(position.status_time)


def leg_symbol(position: SpreadPosition, leg: SpreadLeg) -> str:
    """The leg's stored contract, or for older records one built from the stored expiration."""
    if leg.contract_id:
        return leg.contract_id
    expiration = date.fromisoformat(position.expiration_date)
    return build_option_symbol(position.underlying, expiration, leg.option_type, leg.strike)


def price_position(
    options_client, position: SpreadPosition, now: datetime
) -> SpreadPosition | None:
    """Return a copy of the position valued at the leg mids.

    Returns None unless every leg has a quote no older than MAX_QUOTE_AGE with
    a positive ask at or above the bid. A missing leg would otherwise drop out of
    the sum and fake a profit or a loss.
    """
    legs = position.get_legs()
    symbols = [leg_symbol(position, leg) for leg in legs]
    quotes = options_client.get_option_latest_quote(
        OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    )

    priced_legs = []
    current_value = 0.0
    for leg, symbol in zip(legs, symbols, strict=True):
        mid = _valid_mid(quotes.get(symbol), now)
        if mid is None:
            logger.warning(f"{position.spread_id}: no valid quote for {symbol}")
            return None
        priced_legs.append(replace(leg, current_price=mid))
        # Sold legs are a liability to buy back; bought legs offset it.
        current_value += mid if leg.side == "sell" else -mid

    priced = replace(
        position,
        current_value=round(current_value, 4),
        unrealized_pnl=pnl_usd(position.net_credit, current_value, position.quantity),
    )
    priced.set_legs(priced_legs)
    return priced


def _valid_mid(quote, now: datetime) -> float | None:
    if quote is None or quote.bid_price is None or quote.ask_price is None:
        return None
    if quote.ask_price <= 0 or quote.bid_price < 0 or quote.ask_price < quote.bid_price:
        return None
    if quote.timestamp is None or now - quote.timestamp > MAX_QUOTE_AGE:
        return None
    return (quote.bid_price + quote.ask_price) / 2


def _check_thresholds(position: SpreadPosition) -> str | None:
    """Check if position has hit stop loss or take profit.

    Stop loss: current_value >= stop_loss_price (spread has widened).
    Take profit: current_value <= take_profit_price (spread has narrowed).

    Returns:
        Close reason string or None if no threshold breached.
    """
    if position.current_value >= position.stop_loss_price:
        return "stop"

    if position.take_profit_price > 0 and position.current_value <= position.take_profit_price:
        return "profit"

    return None


def _try_error_alert(source: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, source, error)
    except Exception:  # noqa: BLE001 alerting must not mask the original error
        logger.warning("Failed to publish error alert")
