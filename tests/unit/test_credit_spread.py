"""Tests for directional credit spread construction."""

import pytest

from common.models import MarketRegime
from strategy.strike_selection import OptionQuote
from strategy.credit_spread import build_credit_spread, CreditSpreadResult


def _put_chain() -> list[OptionQuote]:
    return [
        OptionQuote(strike=540.0, delta=-0.05, bid=0.10, ask=0.12, option_type="put"),
        OptionQuote(strike=542.0, delta=-0.08, bid=0.18, ask=0.22, option_type="put"),
        OptionQuote(strike=544.0, delta=-0.12, bid=0.30, ask=0.35, option_type="put"),
        OptionQuote(strike=546.0, delta=-0.16, bid=0.48, ask=0.55, option_type="put"),
        OptionQuote(strike=548.0, delta=-0.22, bid=0.72, ask=0.80, option_type="put"),
    ]


def _call_chain() -> list[OptionQuote]:
    return [
        OptionQuote(strike=552.0, delta=0.22, bid=0.72, ask=0.80, option_type="call"),
        OptionQuote(strike=554.0, delta=0.16, bid=0.48, ask=0.55, option_type="call"),
        OptionQuote(strike=556.0, delta=0.12, bid=0.30, ask=0.35, option_type="call"),
        OptionQuote(strike=558.0, delta=0.08, bid=0.18, ask=0.22, option_type="call"),
        OptionQuote(strike=560.0, delta=0.05, bid=0.10, ask=0.12, option_type="call"),
    ]


class TestBuildCreditSpread:
    def test_uptrend_builds_put_credit_spread(self):
        """Uptrend: sell put spread (bullish, collect credit below)."""
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result is not None
        assert result.spread_type == "put_credit_spread"
        assert len(result.legs) == 2
        assert all(l.option_type == "put" for l in result.legs)

    def test_downtrend_builds_call_credit_spread(self):
        """Downtrend: sell call spread (bearish, collect credit above)."""
        result = build_credit_spread(
            regime=MarketRegime.DOWNTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result is not None
        assert result.spread_type == "call_credit_spread"
        assert len(result.legs) == 2
        assert all(l.option_type == "call" for l in result.legs)

    def test_range_regime_returns_none(self):
        """Range days use iron condors, not directional spreads."""
        result = build_credit_spread(
            regime=MarketRegime.RANGE,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result is None

    def test_uncertain_regime_returns_none(self):
        result = build_credit_spread(
            regime=MarketRegime.UNCERTAIN,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result is None

    def test_net_credit_positive(self):
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result.net_credit > 0

    def test_max_loss_calculation(self):
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result.max_loss == pytest.approx(2.0 - result.net_credit, abs=0.01)

    def test_sell_and_buy_legs(self):
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        sell_legs = [l for l in result.legs if l.side == "sell"]
        buy_legs = [l for l in result.legs if l.side == "buy"]
        assert len(sell_legs) == 1
        assert len(buy_legs) == 1

    def test_min_credit_filter(self):
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=99.0,
        )
        assert result is None

    def test_result_is_frozen(self):
        result = build_credit_spread(
            regime=MarketRegime.UPTREND,
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        with pytest.raises(AttributeError):
            result.net_credit = 999.0
