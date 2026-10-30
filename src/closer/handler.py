"""Closer Lambda: force close all open positions at 3:45 PM ET.

Runs once near end of day as a backstop for the monitor, which already closes
open positions once the session's close deadline has passed (earlier on an
early close day). Positions whose close is not confirmed by a fill stay active
and are reported over the error topic.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from common.config import get_sns_error_topic
from common.notifications import publish_error_alert
from monitor.handler import process_active_positions

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")
SETTLED_OUTCOMES = {"closed", "entry_failed"}


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Closes all open positions with reason "eod". Failures raise, so the
    asynchronous invocation is retried.

    Returns:
        Dict with count of positions closed.
    """
    try:
        return _close_all()
    except Exception as e:
        logger.exception("Closer failed")
        _try_error_alert("closer", str(e))
        raise


def _close_all() -> dict:
    """Close every open position and report positions that remain active."""
    result = process_active_positions(force_close=True)
    now_et = datetime.now(ET)

    if result.get("skipped"):
        message = f"market closed with {result['positions_active']} active position(s)"
        logger.error(message)
        _try_error_alert("closer", message)
        return {"positions_closed": 0, "positions_active": result["positions_active"]}

    checked = result.get("positions_checked", 0)
    closed = result.get("closed", 0)
    still_active = checked - sum(result.get(outcome, 0) for outcome in SETTLED_OUTCOMES)
    if still_active > 0:
        _try_error_alert("closer", f"{still_active} position(s) still active after the close")

    logger.info(f"EOD closer finished at {now_et.strftime('%H:%M:%S ET')}: closed {closed}")
    return {
        "positions_closed": closed,
        "positions_active": still_active,
        "close_time": now_et.isoformat(),
    }


def _try_error_alert(source: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, source, error)
    except Exception:  # noqa: BLE001 alerting must not mask the original error
        logger.warning("Failed to publish error alert")
