"""Tests for scanner Lambda handler decision logic."""

from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from common.models import MarketRegime
from market.regime import RegimeResult, RegimeSignals
from scanner.handler import _check_regime_alignment, _no_trade, _scan

ET = ZoneInfo("US/Eastern")


class TestCheckRegimeAlignment:
    def test_ic_spy_matches_range(self):
        regime = _make_regime(MarketRegime.RANGE, 0.8)
        assert _check_regime_alignment("IC_SPY", regime) is True

    def test_ic_spy_rejects_uptrend(self):
        regime = _make_regime(MarketRegime.UPTREND, 0.8)
        assert _check_regime_alignment("IC_SPY", regime) is False

    def test_cs_spy_matches_uptrend(self):
        regime = _make_regime(MarketRegime.UPTREND, 0.8)
        assert _check_regime_alignment("CS_SPY", regime) is True

    def test_cs_spy_matches_downtrend(self):
        regime = _make_regime(MarketRegime.DOWNTREND, 0.8)
        assert _check_regime_alignment("CS_SPY", regime) is True

    def test_cs_spy_rejects_range(self):
        regime = _make_regime(MarketRegime.RANGE, 0.8)
        assert _check_regime_alignment("CS_SPY", regime) is False

    def test_uncertain_rejected_by_both(self):
        regime = _make_regime(MarketRegime.UNCERTAIN, 0.3)
        assert _check_regime_alignment("IC_SPY", regime) is False
        assert _check_regime_alignment("CS_SPY", regime) is False

    def test_unknown_strategy_rejects(self):
        regime = _make_regime(MarketRegime.RANGE, 0.8)
        assert _check_regime_alignment("UNKNOWN", regime) is False


class TestNoTrade:
    def test_basic_no_trade(self):
        result = _no_trade("IC_SPY", "market_closed")
        assert result["should_trade"] is False
        assert result["strategy"] == "IC_SPY"
        assert result["reason"] == "market_closed"

    def test_no_trade_with_regime(self):
        regime = _make_regime(MarketRegime.RANGE, 0.75)
        result = _no_trade("IC_SPY", "test", regime=regime, spy_price=550.0)
        assert result["regime"] == "range"
        assert result["confidence"] == 0.75
        assert result["spy_price"] == 550.0

    def test_no_trade_omits_zero_values(self):
        result = _no_trade("IC_SPY", "test")
        assert "spy_price" not in result
        assert "vix_level" not in result
        assert "expected_move" not in result


class TestScanIntegration:
    """Integration tests for _scan with mocked external dependencies."""

    @patch("scanner.handler.get_sns_error_topic", return_value="arn:aws:sns:test")
    @patch("scanner.handler.get_market_state_table", return_value="market-state")
    @patch("scanner.handler.get_positions_table", return_value="positions")
    @patch("scanner.handler.get_positions", return_value={})
    @patch("scanner.handler._fetch_expected_move", return_value=3.5)
    @patch("scanner.handler._fetch_vix", return_value=18.5)
    @patch("scanner.handler._save_market_state")
    @patch("scanner.handler.classify_regime")
    @patch("scanner.handler.create_stock_data_client")
    @patch("scanner.handler.create_trading_client")
    @patch("scanner.handler.is_market_open", return_value=(True, MagicMock()))
    def test_trade_signal_on_range_day(
        self,
        mock_open,
        mock_trading,
        mock_stock,
        mock_classify,
        mock_save,
        mock_vix,
        mock_em,
        mock_pos,
        mock_pos_table,
        mock_mkt_table,
        mock_sns,
    ):
        mock_classify.return_value = _make_regime(MarketRegime.RANGE, 0.8)
        mock_stock.return_value = _mock_stock_client()

        from common.config import IC_SPY_CONFIG

        result = _scan(IC_SPY_CONFIG, "IC_SPY")

        assert result["should_trade"] is True
        assert result["regime"] == "range"
        assert result["spy_price"] > 0

    @patch("scanner.handler.get_market_state_table", return_value="market-state")
    @patch("scanner.handler.get_positions_table", return_value="positions")
    @patch("scanner.handler.get_positions", return_value={})
    @patch("scanner.handler._fetch_expected_move", return_value=3.5)
    @patch("scanner.handler._fetch_vix", return_value=18.5)
    @patch("scanner.handler._save_market_state")
    @patch("scanner.handler.classify_regime")
    @patch("scanner.handler.create_stock_data_client")
    @patch("scanner.handler.create_trading_client")
    @patch("scanner.handler.is_market_open", return_value=(True, MagicMock()))
    def test_no_trade_on_regime_mismatch(
        self,
        mock_open,
        mock_trading,
        mock_stock,
        mock_classify,
        mock_save,
        mock_vix,
        mock_em,
        mock_pos,
        mock_pos_table,
        mock_mkt_table,
    ):
        # IC_SPY should not trade on uptrend days
        mock_classify.return_value = _make_regime(MarketRegime.UPTREND, 0.8)
        mock_stock.return_value = _mock_stock_client()

        from common.config import IC_SPY_CONFIG

        result = _scan(IC_SPY_CONFIG, "IC_SPY")

        assert result["should_trade"] is False
        assert "regime_mismatch" in result["reason"]

    @patch("scanner.handler.create_trading_client")
    @patch("scanner.handler.is_market_open", return_value=(False, MagicMock()))
    def test_no_trade_when_market_closed(self, mock_open, mock_trading):
        from common.config import IC_SPY_CONFIG

        result = _scan(IC_SPY_CONFIG, "IC_SPY")

        assert result["should_trade"] is False
        assert result["reason"] == "market_closed"

    @patch("scanner.handler.get_market_state_table", return_value="market-state")
    @patch("scanner.handler.get_positions_table", return_value="positions")
    @patch("scanner.handler.get_positions", return_value={})
    @patch("scanner.handler._fetch_expected_move", return_value=3.5)
    @patch("scanner.handler._fetch_vix", return_value=18.5)
    @patch("scanner.handler._save_market_state")
    @patch("scanner.handler.classify_regime")
    @patch("scanner.handler.create_stock_data_client")
    @patch("scanner.handler.create_trading_client")
    @patch("scanner.handler.is_market_open", return_value=(True, MagicMock()))
    def test_no_trade_on_low_confidence(
        self,
        mock_open,
        mock_trading,
        mock_stock,
        mock_classify,
        mock_save,
        mock_vix,
        mock_em,
        mock_pos,
        mock_pos_table,
        mock_mkt_table,
    ):
        mock_classify.return_value = _make_regime(MarketRegime.RANGE, 0.3)
        mock_stock.return_value = _mock_stock_client()

        from common.config import IC_SPY_CONFIG

        result = _scan(IC_SPY_CONFIG, "IC_SPY")

        assert result["should_trade"] is False
        assert "low_confidence" in result["reason"]


# --- Helpers ---


def _make_regime(regime: MarketRegime, confidence: float) -> RegimeResult:
    signals = RegimeSignals(
        vwap_crosses=2,
        vwap_position="above",
        or_breakout="inside",
        candle_body_ratio=0.5,
        vix_level=18.0,
    )
    return RegimeResult(regime=regime, confidence=confidence, signals=signals)


def _mock_stock_client():
    """Create a mock stock data client that returns realistic bars."""
    client = MagicMock()
    n = 30
    now = datetime.now(ET)
    idx = pd.date_range(
        now.replace(hour=9, minute=30, second=0, microsecond=0),
        periods=n,
        freq="1min",
        tz="US/Eastern",
    )
    bars_df = pd.DataFrame(
        {
            "open": [550.0 + i * 0.1 for i in range(n)],
            "high": [550.5 + i * 0.1 for i in range(n)],
            "low": [549.5 + i * 0.1 for i in range(n)],
            "close": [550.2 + i * 0.1 for i in range(n)],
            "volume": [10000] * n,
        },
        index=idx,
    )

    mock_response = MagicMock()
    mock_response.df = bars_df
    client.get_stock_bars.return_value = mock_response
    return client


class TestHandlerFailures:
    def test_operational_failure_raises_after_the_alert(self):
        import pytest

        from scanner import handler as scanner

        with (
            patch.object(scanner, "_scan", side_effect=ConnectionError("data API unavailable")),
            patch.object(scanner, "_try_error_alert") as alert,
            pytest.raises(ConnectionError),
        ):
            scanner.handler({"strategy": "IC_SPY"}, None)
        alert.assert_called_once()
