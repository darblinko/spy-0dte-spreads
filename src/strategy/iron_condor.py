"""Iron condor construction: sell put spread + call spread on range days."""

from dataclasses import dataclass

from .strike_selection import OptionQuote, find_spread_strikes


@dataclass(frozen=True)
class SpreadLeg:
    """A single leg of a spread for construction purposes."""

    strike: float
    delta: float
    mid_price: float
    option_type: str  # "put" or "call"
    side: str  # "sell" or "buy"


@dataclass(frozen=True)
class IronCondorResult:
    """Result of iron condor construction."""

    legs: tuple[SpreadLeg, ...]
    net_credit: float
    max_loss: float
    spread_width: float


def build_iron_condor(
    put_chain: list[OptionQuote],
    call_chain: list[OptionQuote],
    delta_min: float,
    delta_max: float,
    spread_width: float,
    min_credit: float,
) -> IronCondorResult | None:
    """Build an iron condor from put and call chains.

    Sells a put spread below the market and a call spread above.

    Args:
        put_chain: OTM put options chain.
        call_chain: OTM call options chain.
        delta_min: Min absolute delta for short strikes.
        delta_max: Max absolute delta for short strikes.
        spread_width: Points between short and long strikes.
        min_credit: Minimum acceptable combined credit.

    Returns:
        IronCondorResult or None if construction fails.
    """
    short_put, long_put = find_spread_strikes(put_chain, delta_min, delta_max, spread_width, "put")
    if short_put is None or long_put is None:
        return None

    short_call, long_call = find_spread_strikes(
        call_chain, delta_min, delta_max, spread_width, "call"
    )
    if short_call is None or long_call is None:
        return None

    net_credit = (
        short_put.mid_price + short_call.mid_price - long_put.mid_price - long_call.mid_price
    )

    if net_credit < min_credit:
        return None

    max_loss = spread_width - net_credit

    legs = (
        SpreadLeg(short_put.strike, short_put.delta, short_put.mid_price, "put", "sell"),
        SpreadLeg(long_put.strike, long_put.delta, long_put.mid_price, "put", "buy"),
        SpreadLeg(short_call.strike, short_call.delta, short_call.mid_price, "call", "sell"),
        SpreadLeg(long_call.strike, long_call.delta, long_call.mid_price, "call", "buy"),
    )

    return IronCondorResult(
        legs=legs,
        net_credit=round(net_credit, 4),
        max_loss=round(max_loss, 4),
        spread_width=spread_width,
    )
