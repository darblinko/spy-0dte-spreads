"""Tests for closer Lambda handler."""

from unittest.mock import patch, MagicMock

import pytest

from common.models import SpreadPosition, SpreadType, PositionStatus
from closer.handler import _close_all


def _make_position(spread_id: str = "test-123",
                   strategy: str = "IC_SPY") -> SpreadPosition:
    return SpreadPosition(
        spread_id=spread_id,
        spread_type=SpreadType.IRON_CONDOR.value,
        underlying="SPY",
        entry_time="2026-04-04T10:00:00",
        expiration_date="2026-04-04",
        strategy=strategy,
        net_credit=0.50,
        current_value=0.30,
        stop_loss_price=1.00,
        take_profit_price=0.25,
        status="open",
    )


# Closer delegates to monitor._close_position which uses monitor.handler's imports.
# Patch at monitor.handler where the functions are actually looked up.
_M = "monitor.handler"
_C = "closer.handler"


class TestCloseAll:
    @patch(f"{_C}.get_positions_table", return_value="positions")
    @patch(f"{_C}.get_all_open_positions", return_value=[])
    def test_no_positions(self, mock_get, mock_table):
        result = _close_all()
        assert result["positions_closed"] == 0

    @patch(f"{_M}.publish_trade_alert")
    @patch(f"{_M}.get_sns_trade_topic", return_value="arn:test")
    @patch(f"{_M}.get_trades_table", return_value="trades")
    @patch(f"{_M}.get_positions_table", return_value="positions")
    @patch(f"{_M}.record_trade")
    @patch(f"{_M}._submit_close_orders", return_value=True)
    @patch(f"{_M}.put_position")
    @patch(f"{_C}._update_position_prices")
    @patch(f"{_C}.get_positions_table", return_value="positions")
    @patch(f"{_C}.get_all_open_positions")
    def test_closes_all_positions(self, mock_get, mock_table, mock_update,
                                   mock_put, mock_close_orders, mock_rec,
                                   mock_m_pos_table, mock_trades_table,
                                   mock_sns, mock_alert):
        positions = [
            _make_position("spread-1", "IC_SPY"),
            _make_position("spread-2", "CS_SPY"),
        ]
        mock_get.return_value = positions
        mock_update.side_effect = positions

        result = _close_all()

        assert result["positions_closed"] == 2
        assert "close_time" in result

    @patch(f"{_M}.publish_trade_alert")
    @patch(f"{_M}.get_sns_trade_topic", return_value="arn:test")
    @patch(f"{_M}.get_trades_table", return_value="trades")
    @patch(f"{_M}.get_positions_table", return_value="positions")
    @patch(f"{_M}.record_trade")
    @patch(f"{_M}._submit_close_orders", return_value=True)
    @patch(f"{_M}.put_position")
    @patch(f"{_C}._update_position_prices")
    @patch(f"{_C}.get_positions_table", return_value="positions")
    @patch(f"{_C}.get_all_open_positions")
    def test_sets_eod_status(self, mock_get, mock_table, mock_update,
                              mock_put, mock_close_orders, mock_rec,
                              mock_m_pos_table, mock_trades_table,
                              mock_sns, mock_alert):
        pos = _make_position()
        mock_get.return_value = [pos]
        mock_update.return_value = pos

        _close_all()

        assert pos.status == PositionStatus.CLOSED_EOD.value

    @patch(f"{_M}.publish_trade_alert")
    @patch(f"{_M}.get_sns_trade_topic", return_value="arn:test")
    @patch(f"{_M}.get_trades_table", return_value="trades")
    @patch(f"{_M}.get_positions_table", return_value="positions")
    @patch(f"{_M}.record_trade")
    @patch(f"{_M}._submit_close_orders", return_value=True)
    @patch(f"{_M}.put_position")
    @patch(f"{_C}._update_position_prices")
    @patch(f"{_C}.get_positions_table", return_value="positions")
    @patch(f"{_C}.get_all_open_positions")
    def test_closes_even_if_price_update_fails(self, mock_get, mock_table,
                                                mock_update, mock_put,
                                                mock_close_orders, mock_rec,
                                                mock_m_pos_table,
                                                mock_trades_table, mock_sns,
                                                mock_alert):
        pos = _make_position()
        mock_get.return_value = [pos]
        mock_update.return_value = None  # price fetch failed

        result = _close_all()

        # Should still close with stale prices
        assert result["positions_closed"] == 1
