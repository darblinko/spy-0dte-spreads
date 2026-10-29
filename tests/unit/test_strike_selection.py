"""Tests for option strike selection by delta."""

import pytest

from strategy.strike_selection import (
    OptionQuote,
    find_spread_strikes,
    find_strike_by_delta,
)


def _chain() -> list[OptionQuote]:
    """Simulated SPY put chain, strikes 540-560, deltas decreasing."""
    return [
        OptionQuote(strike=540.0, delta=-0.05, bid=0.10, ask=0.12, option_type="put"),
        OptionQuote(strike=542.0, delta=-0.08, bid=0.18, ask=0.22, option_type="put"),
        OptionQuote(strike=544.0, delta=-0.12, bid=0.30, ask=0.35, option_type="put"),
        OptionQuote(strike=546.0, delta=-0.16, bid=0.48, ask=0.55, option_type="put"),
        OptionQuote(strike=548.0, delta=-0.22, bid=0.72, ask=0.80, option_type="put"),
        OptionQuote(strike=550.0, delta=-0.30, bid=1.10, ask=1.20, option_type="put"),
    ]


def _call_chain() -> list[OptionQuote]:
    """Simulated SPY call chain, strikes 550-570."""
    return [
        OptionQuote(strike=550.0, delta=0.30, bid=1.10, ask=1.20, option_type="call"),
        OptionQuote(strike=552.0, delta=0.22, bid=0.72, ask=0.80, option_type="call"),
        OptionQuote(strike=554.0, delta=0.16, bid=0.48, ask=0.55, option_type="call"),
        OptionQuote(strike=556.0, delta=0.12, bid=0.30, ask=0.35, option_type="call"),
        OptionQuote(strike=558.0, delta=0.08, bid=0.18, ask=0.22, option_type="call"),
        OptionQuote(strike=560.0, delta=0.05, bid=0.10, ask=0.12, option_type="call"),
    ]


class TestFindStrikeByDelta:
    def test_exact_delta_match(self):
        chain = _chain()
        result = find_strike_by_delta(chain, delta_min=0.12, delta_max=0.12)
        assert result is not None
        assert result.strike == 544.0

    def test_delta_range(self):
        chain = _chain()
        result = find_strike_by_delta(chain, delta_min=0.10, delta_max=0.20)
        assert result is not None
        assert result.strike in (544.0, 546.0)

    def test_prefers_closest_to_min_delta(self):
        """Should pick the strike closest to delta_min (furthest OTM)."""
        chain = _chain()
        result = find_strike_by_delta(chain, delta_min=0.10, delta_max=0.20)
        assert result is not None
        # 544 has delta 0.12, 546 has delta 0.16 -- 0.12 is closer to 0.10
        assert result.strike == 544.0

    def test_no_match_returns_none(self):
        chain = _chain()
        result = find_strike_by_delta(chain, delta_min=0.50, delta_max=0.60)
        assert result is None

    def test_empty_chain(self):
        result = find_strike_by_delta([], delta_min=0.10, delta_max=0.20)
        assert result is None

    def test_call_chain(self):
        chain = _call_chain()
        result = find_strike_by_delta(chain, delta_min=0.10, delta_max=0.20)
        assert result is not None
        assert result.strike in (554.0, 556.0)


class TestFindSpreadStrikes:
    def test_put_spread(self):
        chain = _chain()
        short, long = find_spread_strikes(
            chain, delta_min=0.10, delta_max=0.20, spread_width=2.0, option_type="put"
        )
        assert short is not None
        assert long is not None
        # Short should be higher strike (closer to ATM), long is protective
        assert short.strike > long.strike
        assert short.strike - long.strike == pytest.approx(2.0)

    def test_call_spread(self):
        chain = _call_chain()
        short, long = find_spread_strikes(
            chain, delta_min=0.10, delta_max=0.20, spread_width=2.0, option_type="call"
        )
        assert short is not None
        assert long is not None
        # Short should be lower strike (closer to ATM), long is protective
        assert long.strike > short.strike
        assert long.strike - short.strike == pytest.approx(2.0)

    def test_no_matching_long_returns_none(self):
        """If the protective leg strike isn't in the chain, return None."""
        chain = [
            OptionQuote(strike=544.0, delta=-0.12, bid=0.30, ask=0.35, option_type="put"),
        ]
        short, long = find_spread_strikes(
            chain, delta_min=0.10, delta_max=0.20, spread_width=2.0, option_type="put"
        )
        assert short is not None
        assert long is None

    def test_option_quote_mid_price(self):
        q = OptionQuote(strike=550.0, delta=-0.30, bid=1.10, ask=1.20, option_type="put")
        assert q.mid_price == pytest.approx(1.15)

    def test_option_quote_is_frozen(self):
        q = OptionQuote(strike=550.0, delta=-0.30, bid=1.10, ask=1.20, option_type="put")
        with pytest.raises(AttributeError):
            q.strike = 555.0
