"""Reconciler Lambda: end-of-day reconciliation and daily summary.

Runs after the closer. Compares the broker's option positions and working
orders with the stored positions, computes daily P&L in dollars, writes a
summary to S3, and sends a daily report via SNS.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from alpaca.trading.enums import AssetClass, PositionSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from common.alpaca_clients import create_trading_client
from common.config import (
    get_config,
    get_market_state_table,
    get_positions_table,
    get_s3_bucket,
    get_sns_error_topic,
    get_sns_trade_topic,
    get_trades_table,
)
from common.dynamo_io import (
    get_active_positions,
    get_latest_market_state,
    get_trades_for_date,
)
from common.models import CONTRACT_MULTIPLIER, PositionStatus, SpreadPosition
from common.notifications import publish_error_alert
from common.s3_io import write_json_to_s3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Returns:
        Dict with reconciliation results and daily summary.
    """
    try:
        return _reconcile()
    except Exception as e:
        logger.exception("Reconciler failed")
        _try_error_alert("reconciler", str(e))
        raise


def _reconcile() -> dict:
    """Core reconciliation logic."""
    today = datetime.now(ET)
    date_str = today.strftime("%Y-%m-%d")

    # Positions still active in the database after the close
    positions_table = get_positions_table()
    active_positions = get_active_positions(positions_table)
    orphaned_count = len(active_positions)

    if orphaned_count > 0:
        logger.warning(f"Found {orphaned_count} orphaned open positions at EOD")

    trading_client = create_trading_client(get_config("IC_SPY").secret_name)
    discrepancies = _broker_discrepancies(trading_client, active_positions)

    # Gather trades for today
    trades_table = get_trades_table()
    ic_trades = get_trades_for_date(trades_table, "IC_SPY", date_str)
    cs_trades = get_trades_for_date(trades_table, "CS_SPY", date_str)
    all_trades = ic_trades + cs_trades

    # Compute daily summary
    summary = _compute_daily_summary(date_str, all_trades, orphaned_count)
    summary["broker_discrepancies"] = discrepancies

    # Get market state
    market_state_table = get_market_state_table()
    market_state = get_latest_market_state(market_state_table, date_str)
    if market_state:
        summary["market_state"] = market_state

    # Write summary to S3
    bucket = get_s3_bucket()
    s3_key = f"daily-summaries/{date_str}.json"
    write_json_to_s3(bucket, s3_key, summary)

    # Send daily report
    _send_daily_report(summary)

    return summary


def _compute_daily_summary(date_str: str, trades: list[dict], orphaned_count: int) -> dict:
    """Compute aggregate daily statistics from trades."""
    open_trades = [t for t in trades if t.get("action") == "OPEN"]
    close_trades = [t for t in trades if t.get("action") == "CLOSE"]

    total_credits = sum(
        float(t.get("net_credit", 0)) * int(t.get("quantity", 1)) * CONTRACT_MULTIPLIER
        for t in open_trades
    )
    total_pnl = sum(float(t.get("realized_pnl", 0)) for t in close_trades)

    wins = [t for t in close_trades if float(t.get("realized_pnl", 0)) > 0]
    losses = [t for t in close_trades if float(t.get("realized_pnl", 0)) < 0]

    close_reasons = {}
    for t in close_trades:
        reason = t.get("close_reason", "unknown")
        close_reasons[reason] = close_reasons.get(reason, 0) + 1

    return {
        "date": date_str,
        "trades_opened": len(open_trades),
        "trades_closed": len(close_trades),
        "total_credits_collected_usd": round(total_credits, 2),
        "total_realized_pnl_usd": round(total_pnl, 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(close_trades), 4) if close_trades else 0.0,
        "close_reasons": close_reasons,
        "orphaned_positions": orphaned_count,
    }


def _expected_holdings(positions: list[SpreadPosition]) -> dict[str, int]:
    """Signed contract count per symbol implied by stored open and closing positions."""
    holdings: dict[str, int] = {}
    holding_statuses = {PositionStatus.OPEN.value, PositionStatus.CLOSING.value}
    for position in positions:
        if position.status not in holding_statuses:
            continue
        for leg in position.get_legs():
            sign = -1 if leg.side == "sell" else 1
            count = sign * leg.quantity * position.quantity
            holdings[leg.contract_id] = holdings.get(leg.contract_id, 0) + count
    return holdings


def _broker_discrepancies(trading_client, positions: list[SpreadPosition]) -> dict[str, list]:
    """Compare broker option positions and working orders with the stored positions.

    A fill whose database write failed shows up as an unrecorded holding, and a
    position recorded but never filled shows up as a missing holding.
    """
    broker_holdings = {}
    for held in trading_client.get_all_positions():
        if held.asset_class != AssetClass.US_OPTION:
            continue
        sign = -1 if held.side == PositionSide.SHORT else 1
        broker_holdings[held.symbol] = sign * abs(int(float(held.qty)))

    expected = _expected_holdings(positions)
    known_order_ids = {p.spread_id for p in positions} | {
        p.close_client_order_id for p in positions if p.close_client_order_id
    }
    working = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    return {
        "unrecorded_holdings": sorted(s for s in broker_holdings if s not in expected),
        "missing_holdings": sorted(s for s in expected if s not in broker_holdings),
        "quantity_mismatches": sorted(
            s for s in expected if s in broker_holdings and broker_holdings[s] != expected[s]
        ),
        "unrecorded_orders": sorted(
            o.client_order_id for o in working if o.client_order_id not in known_order_ids
        ),
    }


def _send_daily_report(summary: dict) -> None:
    """Send daily summary via SNS."""
    try:
        topic = get_sns_trade_topic()
        pnl = summary["total_realized_pnl_usd"]
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        message = (
            f"Daily Report: {summary['date']}\n"
            f"Realized P&L before fees: {pnl_str}\n"
            f"Trades: {summary['trades_opened']} opened, "
            f"{summary['trades_closed']} closed\n"
            f"Win rate: {summary['win_rate']:.0%}\n"
            f"Close reasons: {summary['close_reasons']}\n"
        )

        if summary["orphaned_positions"] > 0:
            message += f"WARNING: {summary['orphaned_positions']} orphaned positions\n"
        for kind, entries in summary["broker_discrepancies"].items():
            if entries:
                message += f"WARNING: {kind}: {', '.join(entries)}\n"

        from common.notifications import _get_sns

        sns = _get_sns()
        sns.publish(
            TopicArn=topic,
            Subject=f"Premium Daily: {pnl_str} ({summary['date']})",
            Message=message,
        )
    except Exception:  # noqa: BLE001 the summary is already in S3; the report is best effort
        logger.warning("Failed to send daily report")


def _try_error_alert(source: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, source, error)
    except Exception:  # noqa: BLE001 alerting must not mask the original error
        logger.warning("Failed to publish error alert")
