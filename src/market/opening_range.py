"""Opening Range (OR) breakout detection from first 15 minutes of trading."""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    midpoint: float
    width: float


def compute_opening_range(bars: pd.DataFrame, minutes: int = 15) -> OpeningRange | None:
    """Compute the opening range from the first N minutes of bars.

    Args:
        bars: DataFrame with columns: high, low, close.
              Index should be timezone-aware timestamps sorted ascending.
        minutes: Number of minutes for the opening range (default 15).

    Returns:
        OpeningRange dataclass, or None if insufficient data.
    """
    if bars.empty:
        return None

    start_time = bars.index[0]
    end_time = start_time + pd.Timedelta(minutes=minutes)
    or_bars = bars[bars.index < end_time]

    if or_bars.empty:
        return None

    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())
    return OpeningRange(
        high=or_high,
        low=or_low,
        midpoint=(or_high + or_low) / 2,
        width=or_high - or_low,
    )


def classify_or_breakout(current_price: float, opening_range: OpeningRange) -> str:
    """Classify whether price has broken out of the opening range.

    Returns:
        "above": price broke above OR high (bullish)
        "below": price broke below OR low (bearish)
        "inside": price still within OR
    """
    if current_price > opening_range.high:
        return "above"
    if current_price < opening_range.low:
        return "below"
    return "inside"
