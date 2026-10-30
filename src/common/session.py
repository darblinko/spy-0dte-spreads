"""Trading session boundaries derived from the broker clock.

The broker clock reports whether the market is open and when the current session
closes, which covers weekends, holidays and early closes.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import PremiumConfig

ET = ZoneInfo("US/Eastern")
REGULAR_CLOSE = time(16, 0)


def parse_hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def close_deadline(clock, force_close_time: str) -> datetime:
    """Time to force close positions in the current session.

    force_close_time is set for a regular 16:00 close. The same lead time is
    applied to the broker's close, so a 13:00 early close gives 12:45 for a
    15:45 setting.
    """
    lead_time = datetime.combine(date.min, REGULAR_CLOSE) - datetime.combine(
        date.min, parse_hhmm(force_close_time)
    )
    return clock.next_close.astimezone(ET) - max(lead_time, timedelta(0))


def session_now(clock) -> datetime:
    return clock.timestamp.astimezone(ET)


def entry_block_reason(clock, config: PremiumConfig) -> str | None:
    """Reason an entry is not allowed now, or None if it is."""
    if not clock.is_open:
        return "market_closed"
    now = session_now(clock)
    if now.time() < parse_hhmm(config.earliest_entry):
        return "before_entry_window"
    if now.time() > parse_hhmm(config.latest_entry):
        return "after_entry_window"
    if now >= close_deadline(clock, config.force_close_time):
        return "past_close_deadline"
    return None
