"""Tests for opening range breakout detection."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from market.opening_range import OpeningRange, classify_or_breakout, compute_opening_range


def _make_bars(highs, lows, closes, start="2026-04-04 09:30"):
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * len(closes),
        },
        index=pd.date_range(start, periods=len(closes), freq="1min", tz="US/Eastern"),
    )


class TestComputeOpeningRange:
    def test_basic_range(self):
        highs = [525.0, 526.0, 524.0, 525.5, 527.0]
        lows = [523.0, 524.0, 522.0, 523.5, 525.0]
        closes = [524.0, 525.0, 523.0, 524.5, 526.0]
        bars = _make_bars(highs, lows, closes)
        or_result = compute_opening_range(bars, minutes=15)
        assert or_result is not None
        assert or_result.high == 527.0
        assert or_result.low == 522.0
        assert or_result.width == 5.0
        assert or_result.midpoint == 524.5

    def test_empty_bars(self):
        bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        assert compute_opening_range(bars) is None

    def test_single_bar(self):
        bars = _make_bars([525.0], [523.0], [524.0])
        or_result = compute_opening_range(bars, minutes=15)
        assert or_result is not None
        assert or_result.high == 525.0
        assert or_result.low == 523.0

    def test_only_first_15_minutes(self):
        # 20 bars at 1-min frequency, only first 15 should count
        highs = [525.0 + i * 0.1 for i in range(20)]
        lows = [523.0 + i * 0.1 for i in range(20)]
        closes = [524.0 + i * 0.1 for i in range(20)]
        bars = _make_bars(highs, lows, closes)
        or_result = compute_opening_range(bars, minutes=15)
        assert or_result is not None
        # Only first 15 bars should be included
        assert or_result.high == pytest.approx(525.0 + 14 * 0.1, abs=0.01)


class TestClassifyOrBreakout:
    def test_above(self):
        or_range = OpeningRange(high=526.0, low=523.0, midpoint=524.5, width=3.0)
        assert classify_or_breakout(527.0, or_range) == "above"

    def test_below(self):
        or_range = OpeningRange(high=526.0, low=523.0, midpoint=524.5, width=3.0)
        assert classify_or_breakout(522.0, or_range) == "below"

    def test_inside(self):
        or_range = OpeningRange(high=526.0, low=523.0, midpoint=524.5, width=3.0)
        assert classify_or_breakout(524.5, or_range) == "inside"

    def test_at_high_boundary(self):
        or_range = OpeningRange(high=526.0, low=523.0, midpoint=524.5, width=3.0)
        assert classify_or_breakout(526.0, or_range) == "inside"

    def test_at_low_boundary(self):
        or_range = OpeningRange(high=526.0, low=523.0, midpoint=524.5, width=3.0)
        assert classify_or_breakout(523.0, or_range) == "inside"
