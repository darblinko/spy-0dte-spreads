"""Tests for iron condor spread construction."""

import pytest

from strategy.strike_selection import OptionQuote
from strategy.iron_condor import build_iron_condor, IronCondorResult


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


class TestBuildIronCondor:
    def test_successful_build(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        assert result is not None
        assert len(result.legs) == 4
        assert result.net_credit > 0

    def test_four_legs_correct_types(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        put_legs = [l for l in result.legs if l.option_type == "put"]
        call_legs = [l for l in result.legs if l.option_type == "call"]
        assert len(put_legs) == 2
        assert len(call_legs) == 2

    def test_short_legs_are_sell_side(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        sell_legs = [l for l in result.legs if l.side == "sell"]
        buy_legs = [l for l in result.legs if l.side == "buy"]
        assert len(sell_legs) == 2
        assert len(buy_legs) == 2

    def test_net_credit_is_positive(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        # Net credit = short put mid + short call mid - long put mid - long call mid
        assert result.net_credit > 0

    def test_max_loss_calculation(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        # Max loss = spread_width - net_credit (per contract)
        assert result.max_loss == pytest.approx(2.0 - result.net_credit, abs=0.01)

    def test_min_credit_filter(self):
        """If combined credit is below min_credit, return None."""
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=99.0,  # impossibly high
        )
        assert result is None

    def test_empty_put_chain_returns_none(self):
        result = build_iron_condor(
            put_chain=[],
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        assert result is None

    def test_empty_call_chain_returns_none(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=[],
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        assert result is None

    def test_result_is_frozen(self):
        result = build_iron_condor(
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.15,
        )
        with pytest.raises(AttributeError):
            result.net_credit = 999.0
