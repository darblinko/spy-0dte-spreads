"""Tests for monitor Lambda handler."""

from common.models import SpreadPosition, SpreadType
from monitor.handler import _check_thresholds


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
