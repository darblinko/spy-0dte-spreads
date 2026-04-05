"""Monitor Lambda: poll open positions and check stop loss / take profit.

Runs every 5 minutes via Step Functions between 9:35 AM and 4:15 PM ET.
Fetches current option prices, updates unrealized P&L, and triggers
closes when thresholds are breached.
"""

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from common.alpaca_clients import create_options_data_client, create_trading_client
from common.config import (
    get_config,
    get_positions_table,
    get_trades_table,
    get_sns_trade_topic,
    get_sns_error_topic,
)
from common.dynamo_io import get_all_open_positions, put_position, record_trade
from common.models import SpreadPosition, TradeRecord, PositionStatus
from common.notifications import publish_trade_alert, publish_error_alert

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Args:
        event: Optional dict, may contain {"strategy": "IC_SPY"} to filter.
        context: Lambda context (unused).

    Returns:
        Dict with monitor results: positions checked, closed, still open.
    """
    # Guard: only run during market hours (9:35 AM - 4:00 PM ET)
    now_et = datetime.now(ET)
    market_start = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    market_end = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if not (market_start <= now_et <= market_end):
        return {"skipped": True, "reason": "outside_market_hours"}

    try:
        return _monitor(event)
    except Exception as e:
        logger.exception("Monitor failed")
        _try_error_alert("monitor", str(e))
        return {"error": str(e), "positions_checked": 0}


def _monitor(event: dict) -> dict:
    """Core monitor logic."""
    positions_table = get_positions_table()
    open_positions = get_all_open_positions(positions_table)

    if not open_positions:
        return {
            "positions_checked": 0,
            "positions_closed": 0,
            "positions_open": 0,
        }

    closed_count = 0
    checked_count = 0

    for position in open_positions:
        checked_count += 1
        config = get_config(position.strategy)

        # Update current prices
        updated_position = _update_position_prices(config.secret_name, position)
        if updated_position is None:
            logger.warning(f"Could not update prices for {position.spread_id}")
            continue

        # Check thresholds
        close_reason = _check_thresholds(updated_position)

        if close_reason is not None:
            _close_position(updated_position, close_reason, config.secret_name)
            closed_count += 1
        else:
            # Persist updated prices
            put_position(positions_table, updated_position)

    return {
        "positions_checked": checked_count,
        "positions_closed": closed_count,
        "positions_open": checked_count - closed_count,
    }


def _update_position_prices(secret_name: str,
                            position: SpreadPosition) -> Optional[SpreadPosition]:
    """Fetch current option prices and update position P&L.

    Returns a new position with updated current_value and unrealized_pnl,
    or None if price fetch fails.
    """
    try:
        options_client = create_options_data_client(secret_name)
        legs = position.get_legs()
        today_str = datetime.now(ET).strftime("%y%m%d")

        symbols = []
        for leg in legs:
            opt_char = "C" if leg.option_type == "call" else "P"
            symbols.append(f"SPY{today_str}{opt_char}{int(leg.strike):08d}")

        from alpaca.data.requests import OptionLatestQuoteRequest
        quote_request = OptionLatestQuoteRequest(symbol_or_symbols=symbols)
        quotes = options_client.get_option_latest_quote(quote_request)

        current_value = 0.0
        for i, leg in enumerate(legs):
            symbol = symbols[i]
            if symbol not in quotes:
                continue
            quote = quotes[symbol]
            mid = (quote.bid_price + quote.ask_price) / 2
            leg.current_price = mid

            # Sell legs: we gain when price drops. Buy legs: we gain when price rises.
            if leg.side == "sell":
                current_value += mid
            else:
                current_value -= mid

        position.set_legs(legs)
        position.current_value = round(current_value, 4)
        position.unrealized_pnl = round(position.net_credit - current_value, 4)

        return position

    except Exception as e:
        logger.error(f"Failed to update prices for {position.spread_id}: {e}")
        return None


def _check_thresholds(position: SpreadPosition) -> Optional[str]:
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


def _close_position(position: SpreadPosition, reason: str,
                    secret_name: str) -> None:
    """Close a position: submit closing orders, update DB, notify."""
    positions_table = get_positions_table()
    trades_table = get_trades_table()

    # Determine status
    status_map = {
        "stop": PositionStatus.CLOSED_STOP,
        "profit": PositionStatus.CLOSED_PROFIT,
        "eod": PositionStatus.CLOSED_EOD,
    }
    position.status = status_map.get(reason, PositionStatus.CLOSED_MANUAL).value

    # Submit closing orders
    _submit_close_orders(secret_name, position)

    # Update position in DB
    put_position(positions_table, position)

    # Record close trade
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    trade = TradeRecord(
        trade_date=today_str,
        spread_id=position.spread_id,
        strategy=position.strategy,
        action="CLOSE",
        spread_type=position.spread_type,
        underlying=position.underlying,
        net_credit=position.net_credit,
        close_value=position.current_value,
        realized_pnl=position.unrealized_pnl,
        close_reason=reason,
    )
    record_trade(trades_table, trade)

    # Notify
    try:
        topic = get_sns_trade_topic()
        publish_trade_alert(
            topic, position.strategy, f"CLOSE ({reason})",
            position.spread_id, position.spread_type,
            position.unrealized_pnl, position.underlying,
        )
    except Exception:
        logger.warning("Failed to send close notification")

    logger.info(
        f"Closed {position.spread_id}: reason={reason} "
        f"pnl={position.unrealized_pnl:.2f}"
    )


def _submit_close_orders(secret_name: str, position: SpreadPosition) -> bool:
    """Submit a multi-leg closing order (reverse of opening).

    Uses OrderClass.MLEG for atomic fill. limit_price is positive
    (debit) since we're buying back the spread.
    """
    try:
        from alpaca.trading.enums import OrderClass, PositionIntent
        from alpaca.trading.requests import (
            LimitOrderRequest, OptionLegRequest, TimeInForce,
        )

        trading_client = create_trading_client(secret_name)
        legs = position.get_legs()
        today_str = datetime.now(ET).strftime("%y%m%d")

        option_legs = []
        for leg in legs:
            opt_char = "C" if leg.option_type == "call" else "P"
            symbol = f"SPY{today_str}{opt_char}{int(leg.strike):08d}"

            # Reverse: sell->buy_to_close, buy->sell_to_close
            intent = (PositionIntent.BUY_TO_CLOSE if leg.side == "sell"
                      else PositionIntent.SELL_TO_CLOSE)

            option_legs.append(OptionLegRequest(
                symbol=symbol,
                ratio_qty=leg.quantity,
                position_intent=intent,
            ))

        # Positive limit_price = debit paid to close
        order_request = LimitOrderRequest(
            order_class=OrderClass.MLEG,
            qty=1,
            limit_price=position.current_value,
            time_in_force=TimeInForce.DAY,
            legs=option_legs,
        )

        order = trading_client.submit_order(order_request)
        logger.info(
            f"Submitted MLEG close: {position.spread_type} "
            f"debit={position.current_value} order_id={order.id}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to submit close orders: {e}")
        return False


def _try_error_alert(source: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, source, error)
    except Exception:
        logger.warning("Failed to publish error alert")
