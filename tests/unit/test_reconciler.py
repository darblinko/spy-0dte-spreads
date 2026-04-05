"""Tests for reconciler Lambda handler."""

from unittest.mock import patch, MagicMock

import pytest

from reconciler.handler import _compute_daily_summary, _reconcile


class TestComputeDailySummary:
    def test_empty_day(self):
        result = _compute_daily_summary("2026-04-04", [], 0)
        assert result["date"] == "2026-04-04"
        assert result["trades_opened"] == 0
        assert result["trades_closed"] == 0
        assert result["total_realized_pnl"] == 0.0
        assert result["win_rate"] == 0.0
        assert result["orphaned_positions"] == 0

    def test_winning_day(self):
        trades = [
            {"action": "OPEN", "net_credit": 0.50},
            {"action": "OPEN", "net_credit": 0.40},
            {"action": "CLOSE", "realized_pnl": 0.25, "close_reason": "profit"},
            {"action": "CLOSE", "realized_pnl": 0.20, "close_reason": "profit"},
        ]
        result = _compute_daily_summary("2026-04-04", trades, 0)
        assert result["trades_opened"] == 2
        assert result["trades_closed"] == 2
        assert result["total_credits_collected"] == pytest.approx(0.90)
        assert result["total_realized_pnl"] == pytest.approx(0.45)
        assert result["wins"] == 2
        assert result["losses"] == 0
        assert result["win_rate"] == 1.0

    def test_mixed_day(self):
        trades = [
            {"action": "OPEN", "net_credit": 0.50},
            {"action": "CLOSE", "realized_pnl": 0.25, "close_reason": "profit"},
            {"action": "OPEN", "net_credit": 0.40},
            {"action": "CLOSE", "realized_pnl": -0.60, "close_reason": "stop"},
        ]
        result = _compute_daily_summary("2026-04-04", trades, 0)
        assert result["wins"] == 1
        assert result["losses"] == 1
        assert result["win_rate"] == 0.5
        assert result["total_realized_pnl"] == pytest.approx(-0.35)
        assert result["close_reasons"] == {"profit": 1, "stop": 1}

    def test_orphaned_positions_counted(self):
        result = _compute_daily_summary("2026-04-04", [], 3)
        assert result["orphaned_positions"] == 3

    def test_close_reasons_aggregated(self):
        trades = [
            {"action": "CLOSE", "realized_pnl": 0.25, "close_reason": "profit"},
            {"action": "CLOSE", "realized_pnl": 0.15, "close_reason": "profit"},
            {"action": "CLOSE", "realized_pnl": 0.10, "close_reason": "eod"},
        ]
        result = _compute_daily_summary("2026-04-04", trades, 0)
        assert result["close_reasons"]["profit"] == 2
        assert result["close_reasons"]["eod"] == 1


class TestReconcileIntegration:
    @patch("reconciler.handler._send_daily_report")
    @patch("reconciler.handler.write_json_to_s3")
    @patch("reconciler.handler.get_s3_bucket", return_value="test-bucket")
    @patch("reconciler.handler.get_latest_market_state", return_value=None)
    @patch("reconciler.handler.get_market_state_table", return_value="mkt-state")
    @patch("reconciler.handler.get_trades_for_date", return_value=[])
    @patch("reconciler.handler.get_trades_table", return_value="trades")
    @patch("reconciler.handler.get_all_open_positions", return_value=[])
    @patch("reconciler.handler.get_positions_table", return_value="positions")
    def test_empty_day_reconciliation(self, mock_pos_table, mock_pos,
                                       mock_trades_table, mock_trades,
                                       mock_mkt_table, mock_mkt_state,
                                       mock_s3_bucket, mock_s3_write,
                                       mock_report):
        result = _reconcile()

        assert result["trades_opened"] == 0
        assert result["orphaned_positions"] == 0
        mock_s3_write.assert_called_once()
        mock_report.assert_called_once()

    @patch("reconciler.handler._send_daily_report")
    @patch("reconciler.handler.write_json_to_s3")
    @patch("reconciler.handler.get_s3_bucket", return_value="test-bucket")
    @patch("reconciler.handler.get_latest_market_state")
    @patch("reconciler.handler.get_market_state_table", return_value="mkt-state")
    @patch("reconciler.handler.get_trades_for_date")
    @patch("reconciler.handler.get_trades_table", return_value="trades")
    @patch("reconciler.handler.get_all_open_positions", return_value=[])
    @patch("reconciler.handler.get_positions_table", return_value="positions")
    def test_includes_market_state(self, mock_pos_table, mock_pos,
                                    mock_trades_table, mock_trades,
                                    mock_mkt_table, mock_mkt_state,
                                    mock_s3_bucket, mock_s3_write,
                                    mock_report):
        mock_trades.return_value = []
        mock_mkt_state.return_value = {
            "regime": "range",
            "confidence": 0.8,
            "spy_price": 550.0,
        }

        result = _reconcile()

        assert "market_state" in result
        assert result["market_state"]["regime"] == "range"

    @patch("reconciler.handler._send_daily_report")
    @patch("reconciler.handler.write_json_to_s3")
    @patch("reconciler.handler.get_s3_bucket", return_value="test-bucket")
    @patch("reconciler.handler.get_latest_market_state", return_value=None)
    @patch("reconciler.handler.get_market_state_table", return_value="mkt-state")
    @patch("reconciler.handler.get_trades_for_date")
    @patch("reconciler.handler.get_trades_table", return_value="trades")
    @patch("reconciler.handler.get_all_open_positions", return_value=[])
    @patch("reconciler.handler.get_positions_table", return_value="positions")
    def test_s3_key_uses_date(self, mock_pos_table, mock_pos,
                               mock_trades_table, mock_trades,
                               mock_mkt_table, mock_mkt_state,
                               mock_s3_bucket, mock_s3_write,
                               mock_report):
        mock_trades.return_value = []
        _reconcile()

        call_args = mock_s3_write.call_args
        s3_key = call_args[0][1]
        assert s3_key.startswith("daily-summaries/")
        assert s3_key.endswith(".json")
