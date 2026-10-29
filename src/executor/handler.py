"""Executor Lambda: build spread from scan result and submit orders.

Receives scan_result from the scanner Lambda via Step Functions.
Fetches the 0DTE options chain, constructs the spread using the
strategy layer, submits orders to Alpaca, and records the position.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderClass, PositionIntent
from alpaca.trading.requests import (
    LimitOrderRequest,
    OptionLegRequest,
    TimeInForce,
)

from common.alpaca_clients import (
    create_options_data_client,
    create_trading_client,
)
from common.config import (
    get_config,
    get_positions_table,
    get_sns_error_topic,
    get_sns_trade_topic,
    get_trades_table,
)
from common.dynamo_io import put_position, record_trade
from common.models import (
    MarketRegime,
    SpreadLeg,
    SpreadPosition,
    SpreadType,
    TradeRecord,
    generate_spread_id,
)
from common.notifications import publish_error_alert, publish_trade_alert
from strategy.credit_spread import CreditSpreadResult, build_credit_spread
from strategy.iron_condor import IronCondorResult, build_iron_condor
from strategy.strike_selection import OptionQuote

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Args:
        event: Scan result from scanner Lambda. Must include:
            - should_trade: bool
            - strategy: str
            - regime: str
            - spy_price: float
            - vix_level: float
            - expected_move: float
        context: Lambda context (unused).

    Returns:
        Dict with execution results for Step Functions.
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
        return {
            "executed": False,
            "strategy": strategy_name,
            "reason": f"executor_error: {e}",
        }


def _execute(config, scan_result: dict) -> dict:
    """Core execution logic, separated for testability."""
    strategy_name = scan_result["strategy"]
    regime = scan_result["regime"]
    spy_price = scan_result["spy_price"]
    vix_level = scan_result["vix_level"]
    expected_move = scan_result["expected_move"]

    # Fetch 0DTE options chain
    put_chain, call_chain = _fetch_options_chain(config.secret_name, spy_price)

    # Build spread based on regime
    spread_result = _build_spread_for_regime(
        regime=regime,
        put_chain=put_chain,
        call_chain=call_chain,
        delta_min=config.short_delta_min,
        delta_max=config.short_delta_max,
        spread_width=config.spread_width,
        min_credit=config.min_credit,
    )

    if spread_result is None:
        return {
            "executed": False,
            "strategy": strategy_name,
            "reason": "no_spread: could not construct spread from chain",
        }

    # Compute risk levels
    stop_loss, take_profit = _compute_risk_levels(
        spread_result.net_credit,
        config.stop_loss_multiplier,
        config.take_profit_pct,
    )

    # Build position model
    now_et = datetime.now(ET)
    expiration = now_et.strftime("%Y-%m-%d")

    if isinstance(spread_result, IronCondorResult):
        position = _build_position_from_ic(
            ic_result=spread_result,
            strategy=strategy_name,
            underlying=config.underlying,
            expiration=expiration,
            spy_price=spy_price,
            vix=vix_level,
            expected_move=expected_move,
            regime=regime,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        spread_type_str = SpreadType.IRON_CONDOR.value
    else:
        position = _build_position_from_cs(
            cs_result=spread_result,
            strategy=strategy_name,
            underlying=config.underlying,
            expiration=expiration,
            spy_price=spy_price,
            vix=vix_level,
            expected_move=expected_move,
            regime=regime,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        spread_type_str = spread_result.spread_type

    # Submit orders to Alpaca
    submitted = _submit_orders(config.secret_name, position)
    if not submitted:
        return {
            "executed": False,
            "strategy": strategy_name,
            "reason": "order_submission_failed",
        }

    # Persist position
    positions_table = get_positions_table()
    put_position(positions_table, position)

    # Record trade
    trades_table = get_trades_table()
    trade = TradeRecord(
        trade_date=expiration,
        spread_id=position.spread_id,
        strategy=strategy_name,
        action="OPEN",
        spread_type=spread_type_str,
        underlying=config.underlying,
        net_credit=spread_result.net_credit,
        market_regime=regime,
        vix=vix_level,
        spy_price=spy_price,
        expected_move=expected_move,
    )
    record_trade(trades_table, trade)

    # Notify
    topic = get_sns_trade_topic()
    publish_trade_alert(
        topic,
        strategy_name,
        "OPEN",
        position.spread_id,
        spread_type_str,
        spread_result.net_credit,
        config.underlying,
    )

    return {
        "executed": True,
        "strategy": strategy_name,
        "spread_id": position.spread_id,
        "spread_type": spread_type_str,
        "net_credit": spread_result.net_credit,
        "max_loss": spread_result.max_loss,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


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
        (stop_loss_price, take_profit_price) -- spread value thresholds.
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
) -> SpreadPosition:
    """Create a SpreadPosition from an IronCondorResult."""
    spread_id = generate_spread_id()
    now_iso = datetime.now(ET).isoformat()

    legs = [
        SpreadLeg(
            leg_id=f"{spread_id}-{i}",
            contract_id="",  # Filled after order submission
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
) -> SpreadPosition:
    """Create a SpreadPosition from a CreditSpreadResult."""
    spread_id = generate_spread_id()
    now_iso = datetime.now(ET).isoformat()

    legs = [
        SpreadLeg(
            leg_id=f"{spread_id}-{i}",
            contract_id="",
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
    )
    position.set_legs(legs)
    return position


def _fetch_options_chain(
    secret_name: str, spy_price: float
) -> tuple[list[OptionQuote], list[OptionQuote]]:
    """Fetch 0DTE SPY options chain from Alpaca.

    Returns put and call chains as lists of OptionQuote, filtered to OTM only.
    """
    try:
        options_client = create_options_data_client(secret_name)
        today_str = datetime.now(ET).strftime("%y%m%d")

        # Generate OTM strike range: +/- 15 points from current price
        atm = round(spy_price)
        put_strikes = range(atm - 15, atm)
        call_strikes = range(atm + 1, atm + 16)

        # Build option symbols
        put_symbols = [f"SPY{today_str}P{s:08d}" for s in put_strikes]
        call_symbols = [f"SPY{today_str}C{s:08d}" for s in call_strikes]

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

            quote = OptionQuote(
                strike=float(symbol[-8:]) if "P" in symbol else float(symbol[-8:]),
                delta=snap.greeks.delta if snap.greeks.delta else 0.0,
                bid=snap.latest_quote.bid_price or 0.0,
                ask=snap.latest_quote.ask_price or 0.0,
                option_type="put" if "P" in symbol[9:] else "call",
            )

            if quote.bid <= 0 or quote.ask <= 0:
                continue

            if "P" in symbol[9:]:
                put_chain.append(quote)
            else:
                call_chain.append(quote)

        put_chain.sort(key=lambda q: q.strike)
        call_chain.sort(key=lambda q: q.strike)

        logger.info(f"Fetched chain: {len(put_chain)} puts, {len(call_chain)} calls")
        return put_chain, call_chain

    except Exception as e:
        logger.error(f"Failed to fetch options chain: {e}")
        return [], []


def _submit_orders(secret_name: str, position: SpreadPosition) -> bool:
    """Submit a multi-leg order to Alpaca.

    Uses OrderClass.MLEG for atomic fill -- the entire spread fills
    or nothing does. limit_price is negative for credit spreads.
    """
    try:
        trading_client = create_trading_client(secret_name)
        today_str = datetime.now(ET).strftime("%y%m%d")
        legs = position.get_legs()

        option_legs = []
        for leg in legs:
            opt_char = "C" if leg.option_type == "call" else "P"
            symbol = f"SPY{today_str}{opt_char}{int(leg.strike):08d}"

            intent = (
                PositionIntent.SELL_TO_OPEN if leg.side == "sell" else PositionIntent.BUY_TO_OPEN
            )

            option_legs.append(
                OptionLegRequest(
                    symbol=symbol,
                    ratio_qty=leg.quantity,
                    position_intent=intent,
                )
            )

        # Negative limit_price = credit received
        order_request = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            qty=1,
            limit_price=-position.net_credit,
            time_in_force=TimeInForce.DAY,
            legs=option_legs,
        )

        order = trading_client.submit_order(order_request)
        logger.info(
            f"Submitted MLEG order: {position.spread_type} "
            f"credit={position.net_credit} order_id={order.id}"
        )
        return True

    except Exception as e:
        logger.error(f"Order submission failed: {e}")
        return False


def _try_error_alert(strategy_name: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, f"executor/{strategy_name}", error)
    except Exception:
        logger.warning("Failed to publish error alert")
