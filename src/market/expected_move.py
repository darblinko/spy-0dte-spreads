"""Expected move calculation from ATM straddle pricing."""


def compute_expected_move(
    atm_call_price: float, atm_put_price: float, multiplier: float = 0.85
) -> float:
    """Calculate the expected move from ATM straddle price.

    The expected move approximation is the ATM straddle price * multiplier.
    This module uses 0.85 as the default multiplier.

    Args:
        atm_call_price: Price of ATM call option.
        atm_put_price: Price of ATM put option.
        multiplier: Adjustment factor (default 0.85).

    Returns:
        Expected move in points (e.g., 3.5 means +/- $3.50 on SPY).
    """
    straddle_price = atm_call_price + atm_put_price
    return straddle_price * multiplier


def get_expected_range(underlying_price: float, expected_move: float) -> tuple:
    """Get the expected price range for the day.

    Args:
        underlying_price: Current price of the underlying.
        expected_move: Expected move in points.

    Returns:
        Tuple of (lower_bound, upper_bound).
    """
    return (underlying_price - expected_move, underlying_price + expected_move)


def is_strike_outside_expected_move(
    strike: float, underlying_price: float, expected_move: float, option_type: str
) -> bool:
    """Check if a strike is outside the expected move.

    Args:
        strike: Option strike price.
        underlying_price: Current price of the underlying.
        expected_move: Expected move in points.
        option_type: "put" or "call".

    Returns:
        True if strike is safely outside expected move.
    """
    if option_type == "put":
        return strike < (underlying_price - expected_move)
    return strike > (underlying_price + expected_move)
