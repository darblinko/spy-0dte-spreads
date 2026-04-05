"""Tests for market regime detection."""

import os
import sys

import pandas as pd
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from market.regime import classify_regime, compute_candle_body_ratio, RegimeResult
from common.models import MarketRegime


def _make_trend_up_bars(n=30, start_price=525.0):
    """Create bars simulating a strong uptrend."""
    prices = [start_price + i * 0.15 for i in range(n)]
    return pd.DataFrame({
        'open': [p - 0.05 for p in prices],
        'high': [p + 0.10 for p in prices],
        'low': [p - 0.08 for p in prices],
        'close': prices,
        'volume': [10000 + i * 100 for i in range(n)],
    }, index=pd.date_range('2026-04-04 09:30', periods=n, freq='1min', tz='US/Eastern'))


def _make_trend_down_bars(n=30, start_price=525.0):
    """Create bars simulating a strong downtrend."""
    prices = [start_price - i * 0.15 for i in range(n)]
    return pd.DataFrame({
        'open': [p + 0.05 for p in prices],
        'high': [p + 0.08 for p in prices],
        'low': [p - 0.10 for p in prices],
        'close': prices,
        'volume': [10000 + i * 100 for i in range(n)],
    }, index=pd.date_range('2026-04-04 09:30', periods=n, freq='1min', tz='US/Eastern'))


def _make_range_bars(n=30, center=525.0):
    """Create bars simulating choppy range-bound action."""
    np.random.seed(42)
    prices = [center + np.sin(i * 0.5) * 0.5 + np.random.normal(0, 0.1) for i in range(n)]
    return pd.DataFrame({
        'open': prices,
        'high': [p + abs(np.random.normal(0, 0.3)) for p in prices],
        'low': [p - abs(np.random.normal(0, 0.3)) for p in prices],
        'close': [p + np.random.normal(0, 0.05) for p in prices],
        'volume': [10000] * n,
    }, index=pd.date_range('2026-04-04 09:30', periods=n, freq='1min', tz='US/Eastern'))


class TestComputeCandleBodyRatio:
    def test_strong_bodies(self):
        bars = pd.DataFrame({
            'open': [100.0, 101.0, 102.0],
            'high': [101.5, 102.5, 103.5],
            'low': [99.5, 100.5, 101.5],
            'close': [101.0, 102.0, 103.0],
        })
        ratio = compute_candle_body_ratio(bars)
        assert ratio > 0.4  # Bodies are significant vs range

    def test_doji_bars(self):
        bars = pd.DataFrame({
            'open': [100.0, 100.0, 100.0],
            'high': [101.0, 101.0, 101.0],
            'low': [99.0, 99.0, 99.0],
            'close': [100.0, 100.0, 100.0],
        })
        ratio = compute_candle_body_ratio(bars)
        assert ratio < 0.1  # Bodies are tiny vs range

    def test_empty_bars(self):
        bars = pd.DataFrame(columns=['open', 'high', 'low', 'close'])
        ratio = compute_candle_body_ratio(bars)
        assert ratio == 0.5  # Default


class TestClassifyRegime:
    def test_uptrend_detection(self):
        bars = _make_trend_up_bars()
        result = classify_regime(bars, vix_level=18.0)
        assert result.regime == MarketRegime.UPTREND
        assert result.confidence > 0.4

    def test_downtrend_detection(self):
        bars = _make_trend_down_bars()
        result = classify_regime(bars, vix_level=18.0)
        assert result.regime == MarketRegime.DOWNTREND
        assert result.confidence > 0.4

    def test_range_detection(self):
        bars = _make_range_bars()
        result = classify_regime(bars, vix_level=15.0)
        # Range bars should not be classified as a trend
        assert result.regime in (MarketRegime.RANGE, MarketRegime.UNCERTAIN)

    def test_insufficient_data(self):
        bars = pd.DataFrame({
            'open': [525.0],
            'high': [526.0],
            'low': [524.0],
            'close': [525.5],
            'volume': [1000],
        }, index=pd.date_range('2026-04-04 09:30', periods=1, freq='1min', tz='US/Eastern'))
        result = classify_regime(bars, vix_level=18.0)
        assert result.regime == MarketRegime.UNCERTAIN
        assert result.confidence == 0.0

    def test_empty_bars(self):
        bars = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        result = classify_regime(bars, vix_level=18.0)
        assert result.regime == MarketRegime.UNCERTAIN

    def test_result_has_signals(self):
        bars = _make_trend_up_bars()
        result = classify_regime(bars, vix_level=20.0)
        assert result.signals is not None
        assert result.signals.vix_level == 20.0
        assert isinstance(result.signals.vwap_crosses, int)
        assert result.signals.vwap_position in ("above", "below", "at")

    def test_regime_result_is_frozen(self):
        bars = _make_trend_up_bars()
        result = classify_regime(bars, vix_level=18.0)
        with pytest.raises(AttributeError):
            result.regime = MarketRegime.RANGE
