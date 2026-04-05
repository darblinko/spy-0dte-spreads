"""Tests for VWAP calculation."""

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from market.vwap import compute_vwap, count_vwap_crosses, get_current_vwap_position


def _make_bars(prices, volumes):
    """Create simple bars where open=close=high=low for predictable VWAP."""
    return pd.DataFrame({
        'open': prices,
        'high': prices,
        'low': prices,
        'close': prices,
        'volume': volumes,
    }, index=pd.date_range('2026-04-04 09:30', periods=len(prices), freq='1min'))


class TestComputeVwap:
    def test_uniform_price(self):
        bars = _make_bars([100.0] * 5, [1000] * 5)
        vwap = compute_vwap(bars)
        assert all(abs(v - 100.0) < 0.01 for v in vwap)

    def test_volume_weighted(self):
        # Bar 1: price=100, volume=100
        # Bar 2: price=110, volume=900
        # VWAP after bar 2 = (100*100 + 110*900) / (100+900) = 109.0
        bars = _make_bars([100.0, 110.0], [100, 900])
        vwap = compute_vwap(bars)
        assert abs(vwap.iloc[0] - 100.0) < 0.01
        assert abs(vwap.iloc[1] - 109.0) < 0.01

    def test_zero_volume_returns_nan(self):
        bars = _make_bars([100.0], [0])
        vwap = compute_vwap(bars)
        assert pd.isna(vwap.iloc[0])

    def test_ascending_prices(self):
        prices = [520.0, 521.0, 522.0, 523.0, 524.0]
        bars = _make_bars(prices, [1000] * 5)
        vwap = compute_vwap(bars)
        # VWAP should lag the trend (always below in uptrend)
        assert vwap.iloc[-1] < prices[-1]


class TestCountVwapCrosses:
    def test_no_crosses(self):
        prices = pd.Series([101, 102, 103, 104, 105])
        vwap = pd.Series([100, 100, 100, 100, 100])
        assert count_vwap_crosses(prices, vwap) == 0

    def test_one_cross(self):
        prices = pd.Series([101, 102, 99, 98, 97])
        vwap = pd.Series([100, 100, 100, 100, 100])
        assert count_vwap_crosses(prices, vwap) == 1

    def test_multiple_crosses(self):
        prices = pd.Series([101, 99, 101, 99, 101])
        vwap = pd.Series([100, 100, 100, 100, 100])
        assert count_vwap_crosses(prices, vwap) == 4

    def test_empty_series(self):
        prices = pd.Series([], dtype=float)
        vwap = pd.Series([], dtype=float)
        assert count_vwap_crosses(prices, vwap) == 0


class TestGetCurrentVwapPosition:
    def test_above(self):
        assert get_current_vwap_position(525.5, 525.0) == "above"

    def test_below(self):
        assert get_current_vwap_position(524.5, 525.0) == "below"

    def test_at(self):
        assert get_current_vwap_position(525.0, 525.0) == "at"

    def test_very_close_is_at(self):
        # Within 0.01% = "at"
        assert get_current_vwap_position(525.0001, 525.0) == "at"
