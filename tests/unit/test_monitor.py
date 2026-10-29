"""Tests for monitor Lambda handler."""

from unittest.mock import patch

from common.models import PositionStatus, SpreadPosition, SpreadType
from monitor.handler import _check_thresholds, _monitor


def _make_position(
    net_credit: float = 0.50,
    current_value: float = 0.30,
    stop_loss: float = 1.00,
    take_profit: float = 0.25,
    status: str = "open",
) -> SpreadPosition:
    return SpreadPosition(
        spread_id="test-123",
        spread_type=SpreadType.IRON_CONDOR.value,
        underlying="SPY",
        entry_time="2026-04-04T10:00:00",
        expiration_date="2026-04-04",
        strategy="IC_SPY",
        net_credit=net_credit,
        current_value=current_value,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        status=status,
    )


class TestCheckThresholds:
    def test_no_threshold_breached(self):
        pos = _make_position(current_value=0.40)
        assert _check_thresholds(pos) is None

    def test_stop_loss_triggered(self):
        pos = _make_position(current_value=1.00)
        assert _check_thresholds(pos) == "stop"

    def test_stop_loss_exceeded(self):
        pos = _make_position(current_value=1.50)
        assert _check_thresholds(pos) == "stop"

    def test_take_profit_triggered(self):
        pos = _make_position(current_value=0.25)
        assert _check_thresholds(pos) == "profit"

    def test_take_profit_below(self):
        pos = _make_position(current_value=0.10)
        assert _check_thresholds(pos) == "profit"

    def test_zero_take_profit_disabled(self):
        """When take_profit_price is 0, it should not trigger."""
        pos = _make_position(current_value=0.0, take_profit=0.0)
        assert _check_thresholds(pos) is None

    def test_exact_stop_loss_boundary(self):
        pos = _make_position(current_value=1.00, stop_loss=1.00)
        assert _check_thresholds(pos) == "stop"


class TestMonitorIntegration:
    @patch("monitor.handler.get_positions_table", return_value="positions")
    @patch("monitor.handler.get_all_open_positions", return_value=[])
    def test_no_positions(self, mock_get, mock_table):
        result = _monitor({})
        assert result["positions_checked"] == 0
        assert result["positions_closed"] == 0

    @patch("monitor.handler.publish_trade_alert")
    @patch("monitor.handler.get_sns_trade_topic", return_value="arn:test")
    @patch("monitor.handler.get_trades_table", return_value="trades")
    @patch("monitor.handler.record_trade")
    @patch("monitor.handler._submit_close_orders", return_value=True)
    @patch("monitor.handler.put_position")
    @patch("monitor.handler._update_position_prices")
    @patch("monitor.handler.get_positions_table", return_value="positions")
    @patch("monitor.handler.get_all_open_positions")
    def test_closes_on_stop_loss(
        self,
        mock_get,
        mock_table,
        mock_update,
        mock_put,
        mock_close_orders,
        mock_rec,
        mock_trades_table,
        mock_sns,
        mock_alert,
    ):
        pos = _make_position(current_value=1.50, stop_loss=1.00)
        mock_get.return_value = [pos]
        mock_update.return_value = pos

        result = _monitor({})

        assert result["positions_closed"] == 1
        assert pos.status == PositionStatus.CLOSED_STOP.value

    @patch("monitor.handler.put_position")
    @patch("monitor.handler._update_position_prices")
    @patch("monitor.handler.get_positions_table", return_value="positions")
    @patch("monitor.handler.get_all_open_positions")
    def test_keeps_position_open(self, mock_get, mock_table, mock_update, mock_put):
        pos = _make_position(current_value=0.40)
        mock_get.return_value = [pos]
        mock_update.return_value = pos

        result = _monitor({})

        assert result["positions_open"] == 1
        assert result["positions_closed"] == 0
        mock_put.assert_called_once()

    @patch("monitor.handler.put_position")
    @patch("monitor.handler._update_position_prices")
    @patch("monitor.handler.get_positions_table", return_value="positions")
    @patch("monitor.handler.get_all_open_positions")
    def test_skips_failed_price_update(self, mock_get, mock_table, mock_update, mock_put):
        pos = _make_position()
        mock_get.return_value = [pos]
        mock_update.return_value = None

        result = _monitor({})

        assert result["positions_checked"] == 1
        assert result["positions_closed"] == 0
        mock_put.assert_not_called()
