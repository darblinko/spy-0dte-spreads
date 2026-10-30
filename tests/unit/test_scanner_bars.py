"""Scanner bar handling with the alpaca-py BarSet model, not a hand-built frame."""

import pandas as pd
from alpaca.data.models import BarSet

from market.regime import classify_regime
from scanner.handler import _bars_for_symbol


def _barset(symbol: str = "SPY", minutes: int = 30) -> BarSet:
    start = pd.Timestamp("2026-04-06T13:30:00Z")
    raw_bars = []
    # Reverse order checks that the scanner sorts by timestamp.
    for i in reversed(range(minutes)):
        price = 550.0 + (0.2 if i % 4 < 2 else -0.2)
        raw_bars.append(
            {
                "t": (start + pd.Timedelta(minutes=i)).isoformat(),
                "o": price - 0.05,
                "h": price + 0.3,
                "l": price - 0.3,
                "c": price + 0.05,
                "v": 10000 + i,
                "n": 100,
                "vw": price,
            }
        )
    return BarSet({symbol: raw_bars})


def test_barset_frame_has_a_symbol_level():
    assert _barset().df.index.names == ["symbol", "timestamp"]


def test_bars_for_symbol_drops_the_symbol_level_and_sorts():
    bars = _bars_for_symbol(_barset().df, "SPY")
    assert isinstance(bars.index, pd.DatetimeIndex)
    assert bars.index.is_monotonic_increasing
    assert len(bars) == 30


def test_bars_for_symbol_without_rows_is_empty():
    assert _bars_for_symbol(_barset("QQQ").df, "SPY").empty


def test_classification_runs_on_sdk_bars():
    bars = _bars_for_symbol(_barset().df, "SPY")
    result = classify_regime(bars, vix_level=18.0)
    assert 0.0 <= result.confidence <= 1.0
