"""Reconciler Lambda: end-of-day reconciliation and daily summary.

Runs after the closer. Verifies all positions are closed, computes
daily P&L, writes a summary to S3, and sends a daily report via SNS.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from common.config import (
    get_market_state_table,
    get_positions_table,
    get_s3_bucket,
    get_sns_error_topic,
    get_sns_trade_topic,
    get_trades_table,
)
from common.dynamo_io import (
    get_all_open_positions,
    get_latest_market_state,
    get_trades_for_date,
)
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
        return {"error": str(e)}


def _reconcile() -> dict:
    """Core reconciliation logic."""
    today = datetime.now(ET)
    date_str = today.strftime("%Y-%m-%d")

    # Check for orphaned open positions
    positions_table = get_positions_table()
    open_positions = get_all_open_positions(positions_table)
    orphaned_count = len(open_positions)

    if orphaned_count > 0:
        logger.warning(f"Found {orphaned_count} orphaned open positions at EOD")

    # Gather trades for today
    trades_table = get_trades_table()
    ic_trades = get_trades_for_date(trades_table, "IC_SPY", date_str)
    cs_trades = get_trades_for_date(trades_table, "CS_SPY", date_str)
    all_trades = ic_trades + cs_trades

    # Compute daily summary
    summary = _compute_daily_summary(date_str, all_trades, orphaned_count)

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

    total_credits = sum(float(t.get("net_credit", 0)) for t in open_trades)
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
        "total_credits_collected": round(total_credits, 4),
        "total_realized_pnl": round(total_pnl, 4),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(close_trades), 4) if close_trades else 0.0,
        "close_reasons": close_reasons,
        "orphaned_positions": orphaned_count,
    }


def _send_daily_report(summary: dict) -> None:
    """Send daily summary via SNS."""
    try:
        topic = get_sns_trade_topic()
        pnl = summary["total_realized_pnl"]
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

        message = (
            f"Daily Report: {summary['date']}\n"
            f"P&L: {pnl_str}\n"
            f"Trades: {summary['trades_opened']} opened, "
            f"{summary['trades_closed']} closed\n"
            f"Win rate: {summary['win_rate']:.0%}\n"
            f"Close reasons: {summary['close_reasons']}\n"
        )

        if summary["orphaned_positions"] > 0:
            message += f"WARNING: {summary['orphaned_positions']} orphaned positions\n"

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
