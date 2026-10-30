"""Executor Lambda: build spread from scan result and submit orders.

Receives scan_result from the scanner Lambda via Step Functions. Checks the
entry window and risk limits, fetches the 0DTE options chain, constructs the
spread using the strategy layer, reserves the position under a key derived from
the scan, submits the order with that key as client order ID, and records the
position as open only once the broker reports a fill.

A retried invocation with the same scan finds the reservation and the broker
order instead of placing a second order.
"""

import logging
import math
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from common.alpaca_clients import (
    create_options_data_client,
    create_trading_client,
)
from common.config import (
    PremiumConfig,
    get_config,
    get_positions_table,
    get_sns_error_topic,
    get_trades_table,
)
from common.dynamo_io import (
    PositionConflictError,
    create_position,
    get_position,
    get_positions,
    get_trades_for_date,
)
from common.lifecycle import resolve_entry
from common.models import (
    ACTIVE_STATUSES,
    CONTRACT_MULTIPLIER,
    MarketRegime,
    PositionStatus,
    SpreadLeg,
    SpreadPosition,
    SpreadType,
)
from common.notifications import publish_error_alert
from common.option_symbols import build_option_symbol, parse_option_symbol
from common.orders import build_spread_request, submit_or_recover, wait_for_terminal
from common.session import entry_block_reason, session_now
from strategy.credit_spread import CreditSpreadResult, build_credit_spread
from strategy.iron_condor import IronCondorResult, build_iron_condor
from strategy.strike_selection import OptionQuote

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")
ENTRY_FILL_WAIT_SECONDS = 30


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Args:
        event: Scan result from scanner Lambda. Must include:
            - should_trade: bool
            - strategy: str
            - scan_id: str, identifies the scan; the execution key is built from it
            - regime: str
            - spy_price: float
            - vix_level: float
            - expected_move: float
        context: Lambda context (unused).

    Returns:
        Dict with execution results for Step Functions. Operational failures
        raise, so Step Functions retries the task.
    """
    if not event.get("should_trade", False):
        return {"executed": False, "reason": "no_trade_signal"}

    strategy_name = event["strategy"]
    config = get_config(strategy_name)

    try:
        return _execute(config, event)
    except Exception as e:
        logger.exception(f"Executor failed for {strategy_name}")
        _try_error_alert(strategy_name, str(e))
        raise


def _execute(config: PremiumConfig, scan_result: dict) -> dict:
    """Core execution logic, separated for testability."""
    strategy_name = scan_result["strategy"]
    spread_id = _execution_key(scan_result)
    tables = {"positions": get_positions_table(), "trades": get_trades_table()}
    trading_client = create_trading_client(config.secret_name)

    existing = get_position(tables["positions"], strategy_name, spread_id)
    if existing is not None:
        return _resume_entry(trading_client, tables, existing)

    clock = trading_client.get_clock()
    trade_date = session_now(clock).strftime("%Y-%m-%d")
    blocked = entry_block_reason(clock, config) or _risk_block_reason(config, tables, trade_date)
    if blocked:
        return _not_executed(strategy_name, blocked)

    put_chain, call_chain = _fetch_options_chain(config.secret_name, scan_result["spy_price"])
    spread_result = _build_spread_for_regime(
        regime=scan_result["regime"],
        put_chain=put_chain,
        call_chain=call_chain,
        delta_min=config.short_delta_min,
        delta_max=config.short_delta_max,
        spread_width=config.spread_width,
        min_credit=config.min_credit,
    )
    if spread_result is None:
        return _not_executed(strategy_name, "no_spread: could not construct spread from chain")

    equity = float(trading_client.get_account().equity)
    quantity = _position_quantity(
        config, equity, spread_result.max_loss, _remaining_loss_budget(config, tables, trade_date)
    )
    if quantity < 1:
        return _not_executed(strategy_name, "size_below_one_spread")

    position = _build_position(config, scan_result, spread_result, spread_id, quantity, clock)
    try:
        position = create_position(tables["positions"], position)
    except PositionConflictError:
        stored = get_position(tables["positions"], strategy_name, spread_id)
        return _resume_entry(trading_client, tables, stored)
    return _submit_and_settle(trading_client, tables, position)


def _execution_key(scan_result: dict) -> str:
    scan_id = scan_result.get("scan_id")
    if not scan_id:
        raise ValueError("scan result has no scan_id")
    return f"{scan_result['strategy']}-{scan_id}"


def _submit_and_settle(trading_client, tables: dict, position: SpreadPosition) -> dict:
    """Place the entry order (or find it) and record the outcome of the fill."""
    request = build_spread_request(
        position,
        opening=True,
        limit_price=-position.net_credit,
        client_order_id=position.spread_id,
    )
    order = submit_or_recover(trading_client, request)
    order = wait_for_terminal(trading_client, order, ENTRY_FILL_WAIT_SECONDS)
    stored = resolve_entry(trading_client, tables, position, order, cancel_if_working=True)
    return _entry_result(stored)


def _resume_entry(trading_client, tables: dict, position: SpreadPosition) -> dict:
    """Continue an entry that an earlier invocation of the same scan reserved."""
    logger.info(f"Resuming entry {position.spread_id} in status {position.status}")
    if position.status != PositionStatus.PENDING_OPEN.value:
        return _entry_result(position)
    return _submit_and_settle(trading_client, tables, position)


def _entry_result(position: SpreadPosition) -> dict:
    result = {
        "executed": position.status == PositionStatus.OPEN.value,
        "strategy": position.strategy,
        "spread_id": position.spread_id,
        "status": position.status,
    }
    if result["executed"]:
        result.update(
            spread_type=position.spread_type,
            quantity=position.quantity,
            net_credit=position.net_credit,
            max_loss=position.max_loss,
            stop_loss=position.stop_loss_price,
            take_profit=position.take_profit_price,
        )
    else:
        result["reason"] = f"entry_not_filled ({position.status})"
    return result


def _not_executed(strategy_name: str, reason: str) -> dict:
    return {"executed": False, "strategy": strategy_name, "reason": reason}


def _risk_block_reason(config: PremiumConfig, tables: dict, trade_date: str) -> str | None:
    stored = get_positions(tables["positions"], config.name).values()
    active_count = sum(1 for p in stored if p.status in ACTIVE_STATUSES)
    if active_count >= config.max_positions:
        return f"max_positions_reached ({active_count})"
    if _remaining_loss_budget(config, tables, trade_date) <= 0:
        return "daily_loss_limit_reached"
    return None


def _remaining_loss_budget(config: PremiumConfig, tables: dict, trade_date: str) -> float:
    """max_daily_loss less today's realized dollar losses for the strategy."""
    trades = get_trades_for_date(tables["trades"], config.name, trade_date)
    realized = sum(float(t.get("realized_pnl", 0)) for t in trades if t.get("action") == "CLOSE")
    return config.max_daily_loss + min(0.0, realized)


def _position_quantity(
    config: PremiumConfig, equity: float, max_loss_points: float, loss_budget: float
) -> int:
    """Number of spreads: the smaller of the equity share and the remaining daily loss budget.

    Both limits are compared with the maximum loss of one spread in dollars.
    """
    max_loss_usd = max_loss_points * CONTRACT_MULTIPLIER
    if max_loss_usd <= 0:
        return 0
    by_equity = math.floor(equity * config.position_size_pct / max_loss_usd)
    by_loss_budget = math.floor(loss_budget / max_loss_usd)
    return max(0, min(by_equity, by_loss_budget))


def _build_position(
    config: PremiumConfig,
    scan_result: dict,
    spread_result,
    spread_id: str,
    quantity: int,
    clock,
) -> SpreadPosition:
    """Pending position for a 0DTE spread, timestamped with the broker clock."""
    entry_time = session_now(clock)
    stop_loss, take_profit = _compute_risk_levels(
        spread_result.net_credit, config.stop_loss_multiplier, config.take_profit_pct
    )
    builder = (
        _build_position_from_ic
        if isinstance(spread_result, IronCondorResult)
        else _build_position_from_cs
    )
    position = builder(
        spread_result,
        strategy=scan_result["strategy"],
        underlying=config.underlying,
        expiration=entry_time.strftime("%Y-%m-%d"),
        spy_price=scan_result["spy_price"],
        vix=scan_result["vix_level"],
        expected_move=scan_result["expected_move"],
        regime=scan_result["regime"],
        stop_loss=stop_loss,
        take_profit=take_profit,
        spread_id=spread_id,
    )
    entry_iso = entry_time.isoformat()
    return replace(position, quantity=quantity, entry_time=entry_iso, status_time=entry_iso)


def _build_spread_for_regime(
    regime: str,
    put_chain: list[OptionQuote],
    call_chain: list[OptionQuote],
    delta_min: float,
    delta_max: float,
    spread_width: float,
    min_credit: float,
) -> IronCondorResult | CreditSpreadResult | None:
    """Build the appropriate spread type for the detected regime."""
    if regime == "range":
        return build_iron_condor(
            put_chain,
            call_chain,
            delta_min,
            delta_max,
            spread_width,
            min_credit,
        )
    elif regime in ("uptrend", "downtrend"):
        regime_enum = MarketRegime(regime)
        return build_credit_spread(
            regime_enum,
            put_chain,
            call_chain,
            delta_min,
            delta_max,
            spread_width,
            min_credit,
        )
    return None


def _compute_risk_levels(
    net_credit: float, stop_loss_multiplier: float, take_profit_pct: float
) -> tuple[float, float]:
    """Compute stop loss and take profit prices.

    Args:
        net_credit: Credit received per contract.
        stop_loss_multiplier: Close when spread value reaches N * credit.
        take_profit_pct: Take profit at this fraction of credit received.

    Returns:
        (stop_loss_price, take_profit_price), the spread value thresholds.
    """
    stop_loss = round(net_credit * stop_loss_multiplier, 4)
    take_profit = round(net_credit * take_profit_pct, 4)
    return stop_loss, take_profit


def _build_position_from_ic(
    ic_result: IronCondorResult,
    strategy: str,
    underlying: str,
    expiration: str,
    spy_price: float,
    vix: float,
    expected_move: float,
    regime: str,
    stop_loss: float,
    take_profit: float,
    spread_id: str,
) -> SpreadPosition:
    """Create a SpreadPosition from an IronCondorResult."""
    now_iso = datetime.now(ET).isoformat()

    legs = [
        SpreadLeg(
            leg_id=f"{spread_id}-{i}",
            contract_id=build_option_symbol(
                underlying, date.fromisoformat(expiration), leg.option_type, leg.strike
            ),
            side=leg.side,
            option_type=leg.option_type,
            strike=leg.strike,
            quantity=1,
            fill_price=leg.mid_price,
            delta_at_entry=leg.delta,
        )
        for i, leg in enumerate(ic_result.legs)
    ]

    position = SpreadPosition(
        spread_id=spread_id,
        spread_type=SpreadType.IRON_CONDOR.value,
        underlying=underlying,
        entry_time=now_iso,
        expiration_date=expiration,
        strategy=strategy,
        net_credit=ic_result.net_credit,
        max_loss=ic_result.max_loss,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        market_regime=regime,
        vix_at_entry=vix,
        spy_price_at_entry=spy_price,
        expected_move_at_entry=expected_move,
        status=PositionStatus.PENDING_OPEN.value,
        status_time=now_iso,
    )
    position.set_legs(legs)
    return position


def _build_position_from_cs(
    cs_result: CreditSpreadResult,
    strategy: str,
    underlying: str,
    expiration: str,
    spy_price: float,
    vix: float,
    expected_move: float,
    regime: str,
    stop_loss: float,
    take_profit: float,
    spread_id: str,
) -> SpreadPosition:
    """Create a SpreadPosition from a CreditSpreadResult."""
    now_iso = datetime.now(ET).isoformat()

    legs = [
        SpreadLeg(
            leg_id=f"{spread_id}-{i}",
            contract_id=build_option_symbol(
                underlying, date.fromisoformat(expiration), leg.option_type, leg.strike
            ),
            side=leg.side,
            option_type=leg.option_type,
            strike=leg.strike,
            quantity=1,
            fill_price=leg.mid_price,
            delta_at_entry=leg.delta,
        )
        for i, leg in enumerate(cs_result.legs)
    ]

    position = SpreadPosition(
        spread_id=spread_id,
        spread_type=cs_result.spread_type,
        underlying=underlying,
        entry_time=now_iso,
        expiration_date=expiration,
        strategy=strategy,
        net_credit=cs_result.net_credit,
        max_loss=cs_result.max_loss,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        market_regime=regime,
        vix_at_entry=vix,
        spy_price_at_entry=spy_price,
        expected_move_at_entry=expected_move,
        status=PositionStatus.PENDING_OPEN.value,
        status_time=now_iso,
    )
    position.set_legs(legs)
    return position


def _fetch_options_chain(
    secret_name: str, spy_price: float
) -> tuple[list[OptionQuote], list[OptionQuote]]:
    """Fetch 0DTE SPY options chain from Alpaca.

    Returns put and call chains as lists of OptionQuote, filtered to OTM only.
    Data API failures raise so the task is retried.
    """
    options_client = create_options_data_client(secret_name)
    today = datetime.now(ET).date()

    # Generate OTM strike range: +/- 15 points from current price
    atm = round(spy_price)
    put_strikes = range(atm - 15, atm)
    call_strikes = range(atm + 1, atm + 16)

    put_symbols = [build_option_symbol("SPY", today, "put", s) for s in put_strikes]
    call_symbols = [build_option_symbol("SPY", today, "call", s) for s in call_strikes]

    from alpaca.data.requests import OptionSnapshotRequest

    snapshot_request = OptionSnapshotRequest(
        symbol_or_symbols=put_symbols + call_symbols,
    )
    snapshots = options_client.get_option_snapshot(snapshot_request)

    put_chain = []
    call_chain = []

    for symbol, snap in snapshots.items():
        if not snap.latest_quote or not snap.greeks:
            continue

        contract = parse_option_symbol(symbol)
        quote = OptionQuote(
            strike=contract.strike,
            delta=snap.greeks.delta if snap.greeks.delta else 0.0,
            bid=snap.latest_quote.bid_price or 0.0,
            ask=snap.latest_quote.ask_price or 0.0,
            option_type=contract.option_type,
        )

        if quote.bid <= 0 or quote.ask <= 0:
            continue

        if contract.option_type == "put":
            put_chain.append(quote)
        else:
            call_chain.append(quote)

    put_chain.sort(key=lambda q: q.strike)
    call_chain.sort(key=lambda q: q.strike)

    logger.info(f"Fetched chain: {len(put_chain)} puts, {len(call_chain)} calls")
    return put_chain, call_chain


def _try_error_alert(strategy_name: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, f"executor/{strategy_name}", error)
    except Exception:  # noqa: BLE001 alerting must not mask the original error
        logger.warning("Failed to publish error alert")
