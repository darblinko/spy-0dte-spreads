"""Volume-Weighted Average Price calculation from intraday bars."""

import pandas as pd


def compute_vwap(bars: pd.DataFrame) -> pd.Series:
    """Compute cumulative VWAP from OHLCV bars.

    Args:
        bars: DataFrame with columns: high, low, close, volume.
              Index should be timestamps sorted ascending.

    Returns:
        Series of VWAP values aligned to bars index.
    """
    typical_price = (bars['high'] + bars['low'] + bars['close']) / 3
    cum_tp_vol = (typical_price * bars['volume']).cumsum()
    cum_vol = bars['volume'].cumsum()
    # Avoid division by zero for pre-market bars with no volume
    vwap = cum_tp_vol / cum_vol.replace(0, float('nan'))
    return vwap


def count_vwap_crosses(prices: pd.Series, vwap: pd.Series) -> int:
    """Count how many times price crosses VWAP.

    A cross occurs when price moves from above to below VWAP or vice versa.

    Args:
        prices: Series of close prices.
        vwap: Series of VWAP values, same index as prices.

    Returns:
        Number of VWAP crosses.
    """
    above = prices > vwap
    crosses = (above != above.shift(1)).sum()
    # First bar doesn't count as a cross
    return max(0, int(crosses) - 1)


def get_current_vwap_position(price: float, vwap_value: float) -> str:
    """Determine if price is above or below VWAP.

    Returns:
        "above", "below", or "at" (within 0.01% of VWAP).
    """
    if abs(price - vwap_value) / vwap_value < 0.0001:
        return "at"
    return "above" if price > vwap_value else "below"
