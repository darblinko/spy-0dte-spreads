"""Directional credit spread construction for trend days."""

from dataclasses import dataclass
from typing import Optional

from common.models import MarketRegime
from .strike_selection import OptionQuote, find_spread_strikes
from .iron_condor import SpreadLeg


@dataclass(frozen=True)
class CreditSpreadResult:
    """Result of directional credit spread construction."""
    spread_type: str        # "put_credit_spread" or "call_credit_spread"
    legs: tuple[SpreadLeg, ...]
    net_credit: float
    max_loss: float
    spread_width: float


def build_credit_spread(regime: MarketRegime,
                        put_chain: list[OptionQuote],
                        call_chain: list[OptionQuote],
                        delta_min: float,
                        delta_max: float,
                        spread_width: float,
                        min_credit: float) -> Optional[CreditSpreadResult]:
    """Build a directional credit spread based on market regime.

    Uptrend -> sell put credit spread (bullish: collect credit below market).
    Downtrend -> sell call credit spread (bearish: collect credit above market).
    Range/Uncertain -> returns None (use iron condor instead).

    Args:
        regime: Current market regime classification.
        put_chain: OTM put options chain.
        call_chain: OTM call options chain.
        delta_min: Min absolute delta for short strike.
        delta_max: Max absolute delta for short strike.
        spread_width: Points between short and long strikes.
        min_credit: Minimum acceptable credit.

    Returns:
        CreditSpreadResult or None if regime is wrong or construction fails.
    """
    if regime == MarketRegime.UPTREND:
        return _build_put_credit_spread(
            put_chain, delta_min, delta_max, spread_width, min_credit
        )
    elif regime == MarketRegime.DOWNTREND:
        return _build_call_credit_spread(
            call_chain, delta_min, delta_max, spread_width, min_credit
        )
    return None


def _build_put_credit_spread(put_chain: list[OptionQuote],
                             delta_min: float,
                             delta_max: float,
                             spread_width: float,
                             min_credit: float) -> Optional[CreditSpreadResult]:
    short, long = find_spread_strikes(
        put_chain, delta_min, delta_max, spread_width, "put"
    )
    if short is None or long is None:
        return None

    net_credit = short.mid_price - long.mid_price
    if net_credit < min_credit:
        return None

    legs = (
        SpreadLeg(short.strike, short.delta, short.mid_price, "put", "sell"),
        SpreadLeg(long.strike, long.delta, long.mid_price, "put", "buy"),
    )

    return CreditSpreadResult(
        spread_type="put_credit_spread",
        legs=legs,
        net_credit=round(net_credit, 4),
        max_loss=round(spread_width - net_credit, 4),
        spread_width=spread_width,
    )


def _build_call_credit_spread(call_chain: list[OptionQuote],
                              delta_min: float,
                              delta_max: float,
                              spread_width: float,
                              min_credit: float) -> Optional[CreditSpreadResult]:
    short, long = find_spread_strikes(
        call_chain, delta_min, delta_max, spread_width, "call"
    )
    if short is None or long is None:
        return None

    net_credit = short.mid_price - long.mid_price
    if net_credit < min_credit:
        return None

    legs = (
        SpreadLeg(short.strike, short.delta, short.mid_price, "call", "sell"),
        SpreadLeg(long.strike, long.delta, long.mid_price, "call", "buy"),
    )

    return CreditSpreadResult(
        spread_type="call_credit_spread",
        legs=legs,
        net_credit=round(net_credit, 4),
        max_loss=round(spread_width - net_credit, 4),
        spread_width=spread_width,
    )
