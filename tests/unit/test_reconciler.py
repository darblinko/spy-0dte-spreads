"""Tests for the reconciler Lambda handler."""

from unittest.mock import patch

import pytest

from common.dynamo_io import create_position
from reconciler import handler as reconciler
from reconciler.handler import _broker_discrepancies, _compute_daily_summary, _reconcile

from .broker_fakes import FakeTradingClient, make_broker_position, make_clock, make_order
from .test_monitor_lifecycle import LONG_PUT, SHORT_PUT, SPREAD_ID, _position

CLOCK = make_clock("2026-04-06T16:05:00-04:00", "2026-04-07T16:00:00-04:00", is_open=False)


class TestComputeDailySummary:
    def test_empty_day(self):
        result = _compute_daily_summary("2026-04-04", [], 0)
        assert result["date"] == "2026-04-04"
        assert result["trades_opened"] == 0
        assert result["trades_closed"] == 0
        assert result["total_realized_pnl_usd"] == 0.0
        assert result["win_rate"] == 0.0
        assert result["orphaned_positions"] == 0

    def test_credits_are_dollars_for_the_traded_quantity(self):
        trades = [
            {"action": "OPEN", "net_credit": 0.50, "quantity": 2},
            {"action": "OPEN", "net_credit": 0.40},
            {"action": "CLOSE", "realized_pnl": 50.0, "close_reason": "profit"},
            {"action": "CLOSE", "realized_pnl": 20.0, "close_reason": "profit"},
        ]
        result = _compute_daily_summary("2026-04-04", trades, 0)
        assert result["trades_opened"] == 2
        assert result["trades_closed"] == 2
        assert result["total_credits_collected_usd"] == pytest.approx(140.0)
        assert result["total_realized_pnl_usd"] == pytest.approx(70.0)
        assert result["wins"] == 2
        assert result["losses"] == 0
        assert result["win_rate"] == 1.0

    def test_mixed_day(self):
        trades = [
            {"action": "OPEN", "net_credit": 0.50},
            {"action": "CLOSE", "realized_pnl": 25.0, "close_reason": "profit"},
            {"action": "OPEN", "net_credit": 0.40},
            {"action": "CLOSE", "realized_pnl": -60.0, "close_reason": "stop"},
        ]
        result = _compute_daily_summary("2026-04-04", trades, 0)
        assert result["wins"] == 1
        assert result["losses"] == 1
        assert result["win_rate"] == 0.5
        assert result["total_realized_pnl_usd"] == pytest.approx(-35.0)
        assert result["close_reasons"] == {"profit": 1, "stop": 1}

    def test_orphaned_positions_counted(self):
        result = _compute_daily_summary("2026-04-04", [], 3)
        assert result["orphaned_positions"] == 3


class TestBrokerDiscrepancies:
    def test_matching_holdings_report_nothing(self):
        client = FakeTradingClient(CLOCK)
        client.positions = [make_broker_position(SHORT_PUT, -2), make_broker_position(LONG_PUT, 2)]
        result = _broker_discrepancies(client, [_position()])
        assert all(entries == [] for entries in result.values())

    def test_fill_missing_from_the_database_is_unrecorded(self):
        client = FakeTradingClient(CLOCK)
        client.positions = [make_broker_position(SHORT_PUT, -1), make_broker_position(LONG_PUT, 1)]
        result = _broker_discrepancies(client, [])
        assert result["unrecorded_holdings"] == [LONG_PUT, SHORT_PUT]

    def test_recorded_position_without_holdings_is_missing(self):
        result = _broker_discrepancies(FakeTradingClient(CLOCK), [_position()])
        assert result["missing_holdings"] == [LONG_PUT, SHORT_PUT]

    def test_quantity_and_side_mismatches(self):
        client = FakeTradingClient(CLOCK)
        client.positions = [make_broker_position(SHORT_PUT, -1), make_broker_position(LONG_PUT, -2)]
        result = _broker_discrepancies(client, [_position()])
        assert result["quantity_mismatches"] == [LONG_PUT, SHORT_PUT]

    def test_pending_entry_is_not_expected_as_a_holding(self):
        pending = _position(status="pending_open")
        result = _broker_discrepancies(FakeTradingClient(CLOCK), [pending])
        assert result["missing_holdings"] == []

    def test_working_order_unknown_to_the_database(self):
        client = FakeTradingClient(CLOCK)
        client.orders["manual-1"] = make_order("manual-1", "new", 1)
        client.orders[SPREAD_ID] = make_order(SPREAD_ID, "new", 1)
        result = _broker_discrepancies(client, [_position(status="pending_open")])
        assert result["unrecorded_orders"] == ["manual-1"]


@pytest.fixture
def reconcile_env(monkeypatch, dynamo_tables):
    client = FakeTradingClient(CLOCK)
    monkeypatch.setattr(reconciler, "create_trading_client", lambda secret: client)
    monkeypatch.setattr(reconciler, "get_latest_market_state", lambda *a: None)
    monkeypatch.setattr(reconciler, "get_market_state_table", lambda: "mkt-state")
    monkeypatch.setattr(reconciler, "get_s3_bucket", lambda: "test-bucket")
    writes = []
    monkeypatch.setattr(reconciler, "write_json_to_s3", lambda *a: writes.append(a))
    monkeypatch.setattr(reconciler, "_send_daily_report", lambda summary: None)
    return client, writes


class TestReconcile:
    def test_empty_day_writes_a_dated_summary(self, reconcile_env):
        _, writes = reconcile_env
        result = _reconcile()

        assert result["trades_opened"] == 0
        assert result["orphaned_positions"] == 0
        assert len(writes) == 1
        assert writes[0][1].startswith("daily-summaries/")
        assert writes[0][1].endswith(".json")

    def test_orphan_and_broker_holdings_are_reported(self, reconcile_env, dynamo_tables):
        client, _ = reconcile_env
        create_position(dynamo_tables["positions"], _position())
        client.positions = [make_broker_position(SHORT_PUT, -2)]

        result = _reconcile()

        assert result["orphaned_positions"] == 1
        assert result["broker_discrepancies"]["missing_holdings"] == [LONG_PUT]

    def test_includes_market_state(self, reconcile_env, monkeypatch):
        state = {"regime": "range", "confidence": 0.8}
        monkeypatch.setattr(reconciler, "get_latest_market_state", lambda *a: state)
        assert _reconcile()["market_state"]["regime"] == "range"

    def test_failure_raises_after_the_alert(self, reconcile_env):
        with (
            patch.object(reconciler, "_reconcile", side_effect=ConnectionError("down")),
            patch.object(reconciler, "_try_error_alert") as alert,
            pytest.raises(ConnectionError),
        ):
            reconciler.handler({}, None)
        alert.assert_called_once()
