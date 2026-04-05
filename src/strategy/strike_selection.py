"""Option strike selection by delta from an options chain."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    delta: float        # Negative for puts, positive for calls
    bid: float
    ask: float
    option_type: str    # "put" or "call"

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2


def find_strike_by_delta(chain: list[OptionQuote],
                         delta_min: float,
                         delta_max: float) -> Optional[OptionQuote]:
    """Find the option whose absolute delta falls within [delta_min, delta_max].

    Prefers the strike closest to delta_min (furthest OTM, most conservative).

    Args:
        chain: List of OptionQuote for a single expiration and option type.
        delta_min: Minimum absolute delta (e.g., 0.10).
        delta_max: Maximum absolute delta (e.g., 0.20).

    Returns:
        Best matching OptionQuote, or None if no match.
    """
    candidates = [
        q for q in chain
        if delta_min <= abs(q.delta) <= delta_max
    ]
    if not candidates:
        return None
    # Pick the one closest to delta_min (furthest OTM)
    return min(candidates, key=lambda q: abs(abs(q.delta) - delta_min))


def find_spread_strikes(chain: list[OptionQuote],
                        delta_min: float,
                        delta_max: float,
                        spread_width: float,
                        option_type: str) -> tuple[Optional[OptionQuote], Optional[OptionQuote]]:
    """Find short and long strikes for a vertical spread.

    For puts: short strike is higher (closer to ATM), long is lower (protective).
    For calls: short strike is lower (closer to ATM), long is higher (protective).

    Args:
        chain: Options chain for one type (puts or calls).
        delta_min: Min absolute delta for the short strike.
        delta_max: Max absolute delta for the short strike.
        spread_width: Points between short and long strikes.
        option_type: "put" or "call".

    Returns:
        Tuple of (short_leg, long_leg). Either may be None if not found.
    """
    short = find_strike_by_delta(chain, delta_min, delta_max)
    if short is None:
        return None, None

    # Find the protective (long) leg
    if option_type == "put":
        target_strike = short.strike - spread_width
    else:
        target_strike = short.strike + spread_width

    strikes_by_value = {q.strike: q for q in chain}
    long = strikes_by_value.get(target_strike)

    return short, long
