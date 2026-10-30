"""Executor entries against moto DynamoDB and a broker fake returning alpaca-py models."""

import pytest

from common import orders
from common.config import IC_SPY_CONFIG
from common.dynamo_io import get_position, get_trades_for_date, record_trade
from common.models import TradeRecord
from executor import handler as executor

from .broker_fakes import FakeTradingClient, FastClock, make_clock, make_order
from .test_executor import _call_chain, _put_chain

SESSION_CLOSE = "2026-04-06T16:00:00-04:00"
SCAN = {
    "should_trade": True,
    "strategy": "IC_SPY",
    "scan_id": "20260406T100000",
    "regime": "range",
    "confidence": 0.8,
    "expected_move": 3.5,
    "spy_price": 550.0,
    "vix_level": 18.5,
}
SPREAD_ID = "IC_SPY-20260406T100000"


def _filled(request, sell_price: float = 0.40, buy_price: float = 0.15):
    leg_fills = [
        (leg.symbol, "sell" if "sell" in leg.position_intent.value else "buy", None)
        for leg in request.legs
    ]
    leg_fills = [
        (symbol, side, sell_price if side == "sell" else buy_price) for symbol, side, _ in leg_fills
    ]
    return make_order(
        request.client_order_id,
        "filled",
        qty=int(request.qty),
        filled_qty=int(request.qty),
        limit_price=request.limit_price,
        leg_fills=leg_fills,
    )


@pytest.fixture
def broker(monkeypatch, dynamo_tables):
    monkeypatch.setattr(orders, "time", FastClock())
    monkeypatch.setattr(executor, "_fetch_options_chain", lambda *a: (_put_chain(), _call_chain()))
    client = FakeTradingClient(make_clock("2026-04-06T10:00:05-04:00", SESSION_CLOSE))
    monkeypatch.setattr(executor, "create_trading_client", lambda secret: client)
    return client


def _stored():
    return get_position("premium-positions", "IC_SPY", SPREAD_ID)


class TestFilledEntry:
    def test_position_opens_with_the_fill_credit_and_quantity(self, broker):
        broker.on_submit = lambda request: (_filled(request), False)
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result["executed"] is True
        stored = _stored()
        assert stored.status == "open"
        # 2 sold legs at 0.40, 2 bought legs at 0.15
        assert stored.net_credit == pytest.approx(0.50)
        assert stored.quantity == broker.submitted[0].qty
        assert stored.stop_loss_price == pytest.approx(1.00)
        trades = get_trades_for_date("premium-trades", "IC_SPY", "2026-04-06")
        assert [t["action"] for t in trades] == ["OPEN"]

    def test_order_uses_the_execution_key_and_occ_symbols(self, broker):
        broker.on_submit = lambda request: (_filled(request), False)
        executor._execute(IC_SPY_CONFIG, SCAN)

        request = broker.submitted[0]
        assert request.client_order_id == SPREAD_ID
        assert all(leg.symbol.startswith("SPY260406") for leg in request.legs)
        assert all(leg.symbol[-3:] == "000" for leg in request.legs)


class TestUnfilledEntry:
    def test_rejected_order_records_no_open_position(self, broker):
        broker.on_submit = lambda request: (
            make_order(request.client_order_id, "rejected", 1),
            False,
        )
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result["executed"] is False
        assert _stored().status == "entry_failed"
        assert get_trades_for_date("premium-trades", "IC_SPY", "2026-04-06") == []

    def test_accepted_order_without_fill_is_cancelled_not_opened(self, broker):
        broker.on_submit = lambda request: (make_order(request.client_order_id, "new", 1), False)
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result["executed"] is False
        assert broker.canceled == [SPREAD_ID]
        assert _stored().status == "entry_failed"

    def test_order_still_working_after_cancel_stays_pending(self, broker):
        broker.on_submit = lambda request: (make_order(request.client_order_id, "new", 1), False)
        broker.after_cancel[SPREAD_ID] = make_order(SPREAD_ID, "pending_cancel", 1)
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result["executed"] is False
        assert _stored().status == "pending_open"


class TestIdempotency:
    def test_second_delivery_of_the_same_scan_places_no_second_order(self, broker):
        broker.on_submit = lambda request: (_filled(request), False)
        first = executor._execute(IC_SPY_CONFIG, SCAN)
        second = executor._execute(IC_SPY_CONFIG, SCAN)

        assert len(broker.submitted) == 1
        assert first["spread_id"] == second["spread_id"] == SPREAD_ID
        assert second["executed"] is True

    def test_timeout_after_broker_acceptance_recovers_the_order(self, broker):
        broker.on_submit = lambda request: (_filled(request), True)
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result["executed"] is True
        assert len(broker.submitted) == 1

    def test_retry_after_a_crash_between_reservation_and_fill(self, broker):
        broker.on_submit = lambda request: (_filled(request), False)

        def crash(*args, **kwargs):
            raise ConnectionError("lost")

        original = executor.wait_for_terminal
        executor.wait_for_terminal = crash
        try:
            with pytest.raises(ConnectionError):
                executor._execute(IC_SPY_CONFIG, SCAN)
        finally:
            executor.wait_for_terminal = original
        assert _stored().status == "pending_open"

        result = executor._execute(IC_SPY_CONFIG, SCAN)
        assert result["executed"] is True
        assert len(broker.submitted) == 1

    def test_scan_without_scan_id_is_refused(self, broker):
        with pytest.raises(ValueError):
            executor._execute(IC_SPY_CONFIG, {k: v for k, v in SCAN.items() if k != "scan_id"})


class TestRiskControls:
    def test_daily_loss_limit_blocks_entry(self, broker):
        broker.on_submit = lambda request: (_filled(request), False)
        record_trade(
            "premium-trades",
            TradeRecord(
                trade_date="2026-04-06",
                spread_id="IC_SPY-earlier",
                strategy="IC_SPY",
                action="CLOSE",
                spread_type="iron_condor",
                underlying="SPY",
                realized_pnl=-IC_SPY_CONFIG.max_daily_loss,
            ),
        )
        result = executor._execute(IC_SPY_CONFIG, SCAN)

        assert result == {
            "executed": False,
            "strategy": "IC_SPY",
            "reason": "daily_loss_limit_reached",
        }
        assert broker.submitted == []

    def test_signal_after_latest_entry_is_not_executed(self, broker):
        broker.clock = make_clock("2026-04-06T14:31:00-04:00", SESSION_CLOSE)
        result = executor._execute(IC_SPY_CONFIG, SCAN)
        assert result["reason"] == "after_entry_window"

    def test_early_close_deadline_blocks_entry(self, broker):
        broker.clock = make_clock("2026-11-27T12:50:00-05:00", "2026-11-27T13:00:00-05:00")
        result = executor._execute(IC_SPY_CONFIG, SCAN)
        assert result["reason"] == "past_close_deadline"

    def test_quantity_is_the_smaller_of_equity_share_and_loss_budget(self):
        # 0.70 points max loss is $70 per spread
        assert executor._position_quantity(IC_SPY_CONFIG, 100_000, 0.70, 500.0) == 7
        assert executor._position_quantity(IC_SPY_CONFIG, 1_000, 0.70, 500.0) == 0
        assert executor._position_quantity(IC_SPY_CONFIG, 100_000, 0.70, 60.0) == 0


class TestHandlerFailures:
    def test_operational_failure_raises_for_the_workflow_retry(self, broker, monkeypatch):
        def broken_chain(*args):
            raise ConnectionError("data API unavailable")

        monkeypatch.setattr(executor, "_fetch_options_chain", broken_chain)
        with pytest.raises(ConnectionError):
            executor.handler(SCAN, None)
