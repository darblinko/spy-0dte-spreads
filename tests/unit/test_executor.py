"""Tests for executor Lambda handler."""

from unittest.mock import patch

import pytest

from common.models import MarketRegime, PositionStatus, SpreadType
from executor.handler import (
    _build_position_from_cs,
    _build_position_from_ic,
    _build_spread_for_regime,
    _compute_risk_levels,
    _execute,
)
from strategy.credit_spread import CreditSpreadResult
from strategy.iron_condor import IronCondorResult
from strategy.strike_selection import OptionQuote

# --- Fixtures ---


def _put_chain() -> list[OptionQuote]:
    """Put chain with $1 strike spacing to match IC_SPY_CONFIG spread_width=1.0."""
    return [
        OptionQuote(strike=540.0, delta=-0.05, bid=0.08, ask=0.10, option_type="put"),
        OptionQuote(strike=541.0, delta=-0.07, bid=0.12, ask=0.15, option_type="put"),
        OptionQuote(strike=542.0, delta=-0.08, bid=0.18, ask=0.22, option_type="put"),
        OptionQuote(strike=543.0, delta=-0.10, bid=0.25, ask=0.30, option_type="put"),
        OptionQuote(strike=544.0, delta=-0.12, bid=0.30, ask=0.35, option_type="put"),
        OptionQuote(strike=545.0, delta=-0.14, bid=0.38, ask=0.44, option_type="put"),
        OptionQuote(strike=546.0, delta=-0.16, bid=0.48, ask=0.55, option_type="put"),
        OptionQuote(strike=547.0, delta=-0.19, bid=0.60, ask=0.68, option_type="put"),
        OptionQuote(strike=548.0, delta=-0.22, bid=0.72, ask=0.80, option_type="put"),
    ]


def _call_chain() -> list[OptionQuote]:
    """Call chain with $1 strike spacing to match IC_SPY_CONFIG spread_width=1.0."""
    return [
        OptionQuote(strike=552.0, delta=0.22, bid=0.72, ask=0.80, option_type="call"),
        OptionQuote(strike=553.0, delta=0.19, bid=0.60, ask=0.68, option_type="call"),
        OptionQuote(strike=554.0, delta=0.16, bid=0.48, ask=0.55, option_type="call"),
        OptionQuote(strike=555.0, delta=0.14, bid=0.38, ask=0.44, option_type="call"),
        OptionQuote(strike=556.0, delta=0.12, bid=0.30, ask=0.35, option_type="call"),
        OptionQuote(strike=557.0, delta=0.10, bid=0.25, ask=0.30, option_type="call"),
        OptionQuote(strike=558.0, delta=0.08, bid=0.18, ask=0.22, option_type="call"),
        OptionQuote(strike=559.0, delta=0.07, bid=0.12, ask=0.15, option_type="call"),
        OptionQuote(strike=560.0, delta=0.05, bid=0.10, ask=0.12, option_type="call"),
    ]


def _ic_result() -> IronCondorResult:
    from strategy.iron_condor import build_iron_condor

    return build_iron_condor(
        put_chain=_put_chain(),
        call_chain=_call_chain(),
        delta_min=0.10,
        delta_max=0.20,
        spread_width=2.0,
        min_credit=0.10,
    )


def _cs_result() -> CreditSpreadResult:
    from strategy.credit_spread import build_credit_spread

    return build_credit_spread(
        regime=MarketRegime.UPTREND,
        put_chain=_put_chain(),
        call_chain=_call_chain(),
        delta_min=0.10,
        delta_max=0.20,
        spread_width=2.0,
        min_credit=0.10,
    )


# --- Tests ---


class TestBuildSpreadForRegime:
    def test_range_builds_iron_condor(self):
        result = _build_spread_for_regime(
            regime="range",
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert isinstance(result, IronCondorResult)
        assert len(result.legs) == 4

    def test_uptrend_builds_put_credit_spread(self):
        result = _build_spread_for_regime(
            regime="uptrend",
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert isinstance(result, CreditSpreadResult)
        assert result.spread_type == "put_credit_spread"

    def test_downtrend_builds_call_credit_spread(self):
        result = _build_spread_for_regime(
            regime="downtrend",
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert isinstance(result, CreditSpreadResult)
        assert result.spread_type == "call_credit_spread"

    def test_uncertain_returns_none(self):
        result = _build_spread_for_regime(
            regime="uncertain",
            put_chain=_put_chain(),
            call_chain=_call_chain(),
            delta_min=0.10,
            delta_max=0.20,
            spread_width=2.0,
            min_credit=0.10,
        )
        assert result is None


class TestComputeRiskLevels:
    def test_stop_loss_is_double_credit(self):
        stop, _take = _compute_risk_levels(
            net_credit=0.50, stop_loss_multiplier=2.0, take_profit_pct=0.50
        )
        assert stop == pytest.approx(1.0)

    def test_take_profit_is_half_credit(self):
        _stop, take = _compute_risk_levels(
            net_credit=0.50, stop_loss_multiplier=2.0, take_profit_pct=0.50
        )
        assert take == pytest.approx(0.25)

    def test_custom_multipliers(self):
        stop, take = _compute_risk_levels(
            net_credit=1.00, stop_loss_multiplier=3.0, take_profit_pct=0.75
        )
        assert stop == pytest.approx(3.0)
        assert take == pytest.approx(0.75)


class TestBuildPositionFromIC:
    def test_creates_position_with_four_legs(self):
        ic = _ic_result()
        pos = _build_position_from_ic(
            ic_result=ic,
            strategy="IC_SPY",
            underlying="SPY",
            expiration="2026-04-04",
            spy_price=550.0,
            vix=18.5,
            expected_move=3.5,
            regime="range",
            stop_loss=1.0,
            take_profit=0.25,
        )
        assert pos.spread_type == SpreadType.IRON_CONDOR.value
        assert pos.strategy == "IC_SPY"
        assert len(pos.legs) == 4
        assert pos.net_credit == ic.net_credit
        assert pos.max_loss == ic.max_loss
        assert pos.status == PositionStatus.OPEN.value
        assert pos.stop_loss_price == 1.0
        assert pos.take_profit_price == 0.25

    def test_market_context_stored(self):
        ic = _ic_result()
        pos = _build_position_from_ic(
            ic_result=ic,
            strategy="IC_SPY",
            underlying="SPY",
            expiration="2026-04-04",
            spy_price=550.0,
            vix=18.5,
            expected_move=3.5,
            regime="range",
            stop_loss=1.0,
            take_profit=0.25,
        )
        assert pos.spy_price_at_entry == 550.0
        assert pos.vix_at_entry == 18.5
        assert pos.expected_move_at_entry == 3.5
        assert pos.market_regime == "range"


class TestBuildPositionFromCS:
    def test_creates_position_with_two_legs(self):
        cs = _cs_result()
        pos = _build_position_from_cs(
            cs_result=cs,
            strategy="CS_SPY",
            underlying="SPY",
            expiration="2026-04-04",
            spy_price=550.0,
            vix=18.5,
            expected_move=3.5,
            regime="uptrend",
            stop_loss=1.0,
            take_profit=0.25,
        )
        assert pos.spread_type == cs.spread_type
        assert pos.strategy == "CS_SPY"
        assert len(pos.legs) == 2
        assert pos.net_credit == cs.net_credit


class TestExecuteIntegration:
    @patch("executor.handler.get_sns_trade_topic", return_value="arn:aws:sns:test")
    @patch("executor.handler.get_sns_error_topic", return_value="arn:aws:sns:err")
    @patch("executor.handler.get_trades_table", return_value="trades")
    @patch("executor.handler.get_positions_table", return_value="positions")
    @patch("executor.handler.record_trade")
    @patch("executor.handler.put_position")
    @patch("executor.handler.publish_trade_alert")
    @patch("executor.handler._submit_orders")
    @patch("executor.handler._fetch_options_chain")
    def test_successful_ic_execution(
        self,
        mock_chain,
        mock_submit,
        mock_alert,
        mock_put_pos,
        mock_rec_trade,
        mock_pos_table,
        mock_trades_table,
        mock_sns_err,
        mock_sns_trade,
    ):
        mock_chain.return_value = (_put_chain(), _call_chain())
        mock_submit.return_value = True

        from common.config import IC_SPY_CONFIG

        scan_result = {
            "should_trade": True,
            "strategy": "IC_SPY",
            "regime": "range",
            "confidence": 0.8,
            "expected_move": 3.5,
            "spy_price": 550.0,
            "vix_level": 18.5,
        }
        result = _execute(IC_SPY_CONFIG, scan_result)

        assert result["executed"] is True
        assert result["spread_type"] == "iron_condor"
        mock_put_pos.assert_called_once()
        mock_rec_trade.assert_called_once()
        mock_alert.assert_called_once()

    @patch("executor.handler.get_sns_error_topic", return_value="arn:aws:sns:err")
    @patch("executor.handler.get_positions_table", return_value="positions")
    @patch("executor.handler._fetch_options_chain")
    def test_no_execution_when_spread_fails(self, mock_chain, mock_pos_table, mock_sns_err):
        # Empty chains = no spread can be built
        mock_chain.return_value = ([], [])

        from common.config import IC_SPY_CONFIG

        scan_result = {
            "should_trade": True,
            "strategy": "IC_SPY",
            "regime": "range",
            "confidence": 0.8,
            "expected_move": 3.5,
            "spy_price": 550.0,
            "vix_level": 18.5,
        }
        result = _execute(IC_SPY_CONFIG, scan_result)

        assert result["executed"] is False
        assert "no_spread" in result["reason"]


class TestOptionSymbols:
    def test_position_legs_carry_occ_symbols(self):
        pos = _build_position_from_cs(
            cs_result=_cs_result(),
            strategy="CS_SPY",
            underlying="SPY",
            expiration="2026-04-04",
            spy_price=550.0,
            vix=18.5,
            expected_move=3.5,
            regime="uptrend",
            stop_loss=1.0,
            take_profit=0.25,
        )
        for leg in pos.get_legs():
            assert leg.contract_id == f"SPY260404P{int(leg.strike * 1000):08d}"
