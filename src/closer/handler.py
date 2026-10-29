"""Closer Lambda: force close all open positions at 3:45 PM ET.

Runs once near end of day. Closes every remaining open position
with reason "eod" regardless of P&L. This ensures no 0DTE positions
are held through expiration.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from common.config import get_config, get_positions_table, get_sns_error_topic
from common.dynamo_io import get_all_open_positions
from common.notifications import publish_error_alert
from monitor.handler import _close_position, _update_position_prices

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Closes all open positions with reason "eod".

    Returns:
        Dict with count of positions closed.
    """
    try:
        return _close_all()
    except Exception as e:
        logger.exception("Closer failed")
        _try_error_alert("closer", str(e))
        return {"error": str(e), "positions_closed": 0}


def _close_all() -> dict:
    """Close every open position."""
    positions_table = get_positions_table()
    open_positions = get_all_open_positions(positions_table)

    if not open_positions:
        logger.info("No open positions to close at EOD")
        return {"positions_closed": 0}

    closed_count = 0

    for position in open_positions:
        config = get_config(position.strategy)

        # Get final prices before closing
        updated = _update_position_prices(config.secret_name, position)
        target = updated if updated is not None else position

        _close_position(target, "eod", config.secret_name)
        closed_count += 1
        logger.info(f"EOD close: {position.spread_id} pnl={target.unrealized_pnl:.2f}")

    now_et = datetime.now(ET)
    logger.info(
        f"EOD closer finished at {now_et.strftime('%H:%M:%S ET')}: closed {closed_count} positions"
    )

    return {
        "positions_closed": closed_count,
        "close_time": now_et.isoformat(),
    }


def _try_error_alert(source: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, source, error)
    except Exception:  # noqa: BLE001 alerting must not mask the original error
        logger.warning("Failed to publish error alert")
