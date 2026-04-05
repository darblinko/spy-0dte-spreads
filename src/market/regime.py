"""Market regime detection: classify trading day as range, trend, or uncertain."""

from dataclasses import dataclass

import pandas as pd

from .vwap import compute_vwap, count_vwap_crosses, get_current_vwap_position
from .opening_range import compute_opening_range, classify_or_breakout
from common.models import MarketRegime


@dataclass(frozen=True)
class RegimeSignals:
    """Individual signals contributing to regime classification."""
    vwap_crosses: int
    vwap_position: str          # "above", "below", "at"
    or_breakout: str            # "above", "below", "inside"
    candle_body_ratio: float    # 0.0 to 1.0
    vix_level: float


@dataclass(frozen=True)
class RegimeResult:
    """Market regime classification with confidence."""
    regime: MarketRegime
    confidence: float           # 0.0 to 1.0
    signals: RegimeSignals


def compute_candle_body_ratio(bars: pd.DataFrame) -> float:
    """Compute ratio of candle bodies to total range.

    High ratio (>0.6) = trend (strong directional moves).
    Low ratio (<0.4) = range (lots of wicks, indecision).

    Args:
        bars: DataFrame with columns: open, high, low, close.

    Returns:
        Ratio between 0.0 and 1.0.
    """
    if bars.empty:
        return 0.5

    body_sizes = (bars['close'] - bars['open']).abs()
    range_sizes = bars['high'] - bars['low']
    # Avoid division by zero for doji bars
    range_sizes = range_sizes.replace(0, float('nan'))
    ratios = body_sizes / range_sizes
    return float(ratios.mean())


def classify_regime(bars: pd.DataFrame, vix_level: float,
                    vwap_cross_threshold: int = 3,
                    candle_body_trend: float = 0.6,
                    candle_body_range: float = 0.4) -> RegimeResult:
    """Classify market regime from intraday SPY bars.

    Combines multiple signals:
    1. VWAP crosses (>=threshold = range)
    2. Opening range breakout direction
    3. Candle body ratio (trend vs range)
    4. Current price vs VWAP position

    Args:
        bars: SPY 1-min bars from 9:30 to ~10:00 AM.
        vix_level: Current VIX level.
        vwap_cross_threshold: VWAP crosses >= this = range signal.
        candle_body_trend: Body ratio above this = trend signal.
        candle_body_range: Body ratio below this = range signal.

    Returns:
        RegimeResult with regime, confidence, and individual signals.
    """
    if bars.empty or len(bars) < 5:
        return RegimeResult(
            regime=MarketRegime.UNCERTAIN,
            confidence=0.0,
            signals=RegimeSignals(0, "at", "inside", 0.5, vix_level),
        )

    vwap = compute_vwap(bars)
    vwap_crosses = count_vwap_crosses(bars['close'], vwap)
    current_price = float(bars['close'].iloc[-1])
    current_vwap = float(vwap.iloc[-1])
    vwap_position = get_current_vwap_position(current_price, current_vwap)

    opening_range = compute_opening_range(bars)
    or_breakout = "inside"
    if opening_range is not None:
        or_breakout = classify_or_breakout(current_price, opening_range)

    body_ratio = compute_candle_body_ratio(bars)

    signals = RegimeSignals(
        vwap_crosses=vwap_crosses,
        vwap_position=vwap_position,
        or_breakout=or_breakout,
        candle_body_ratio=body_ratio,
        vix_level=vix_level,
    )

    # Score each regime hypothesis
    range_score = 0.0
    uptrend_score = 0.0
    downtrend_score = 0.0

    # VWAP crosses: high crosses = range
    if vwap_crosses >= vwap_cross_threshold:
        range_score += 0.35
    elif vwap_crosses <= 1:
        # Low crosses = trend (price staying one side)
        if vwap_position == "above":
            uptrend_score += 0.25
        elif vwap_position == "below":
            downtrend_score += 0.25

    # VWAP position persistence
    if vwap_position == "above":
        uptrend_score += 0.20
    elif vwap_position == "below":
        downtrend_score += 0.20
    else:
        range_score += 0.10

    # Opening range breakout
    if or_breakout == "above":
        uptrend_score += 0.25
    elif or_breakout == "below":
        downtrend_score += 0.25
    elif or_breakout == "inside":
        range_score += 0.20

    # Candle body ratio
    if body_ratio >= candle_body_trend:
        # Strong bodies = trend
        if vwap_position == "above":
            uptrend_score += 0.20
        elif vwap_position == "below":
            downtrend_score += 0.20
        else:
            # Strong bodies but no clear direction
            uptrend_score += 0.05
            downtrend_score += 0.05
    elif body_ratio <= candle_body_range:
        range_score += 0.25

    # Determine winner
    scores = {
        MarketRegime.RANGE: range_score,
        MarketRegime.UPTREND: uptrend_score,
        MarketRegime.DOWNTREND: downtrend_score,
    }
    best_regime = max(scores, key=scores.get)
    best_score = scores[best_regime]
    total = sum(scores.values())
    confidence = best_score / total if total > 0 else 0.0

    if confidence < 0.4:
        return RegimeResult(
            regime=MarketRegime.UNCERTAIN,
            confidence=confidence,
            signals=signals,
        )

    return RegimeResult(
        regime=best_regime,
        confidence=confidence,
        signals=signals,
    )
