"""Tests for DynamoDB access."""

from dataclasses import replace
from decimal import Decimal

import pytest

from common import dynamo_io
from common.models import SpreadPosition


class _PagedTable:
    """Table stub that returns items over several query pages, as DynamoDB does past 1 MB."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        response = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"page": index + 1}
        return response


def _position_item(spread_id: str, status: str) -> dict:
    return {
        "strategy": "IC_SPY",
        "spread_id": spread_id,
        "spread_type": "iron_condor",
        "underlying": "SPY",
        "entry_time": "2026-04-06T10:00:00-04:00",
        "expiration_date": "2026-04-06",
        "status": status,
        "net_credit": Decimal("0.5"),
        "legs": "[]",
    }


class TestPagination:
    def test_query_all_follows_last_evaluated_key(self):
        table = _PagedTable([[{"n": 1}], [{"n": 2}], [{"n": 3}]])
        items = dynamo_io._query_all(table, KeyConditionExpression="k")
        assert [item["n"] for item in items] == [1, 2, 3]
        assert "ExclusiveStartKey" not in table.calls[0]
        assert table.calls[1]["ExclusiveStartKey"] == {"page": 1}
        assert table.calls[2]["ExclusiveStartKey"] == {"page": 2}

    def test_open_position_on_a_later_page_is_returned(self, monkeypatch):
        table = _PagedTable(
            [
                [_position_item(f"closed-{i}", "closed_profit") for i in range(3)],
                [_position_item("open-1", "open")],
            ]
        )
        monkeypatch.setattr(dynamo_io, "_get_table", lambda name: table)
        positions = dynamo_io.get_positions("positions", "IC_SPY")
        assert "open-1" in positions
        assert len(positions) == 4

    def test_trades_on_a_later_page_are_returned(self, monkeypatch):
        table = _PagedTable([[{"trade_id": "a"}], [{"trade_id": "b"}]])
        monkeypatch.setattr(dynamo_io, "_get_table", lambda name: table)
        trades = dynamo_io.get_trades_for_date("trades", "IC_SPY", "2026-04-06")
        assert [t["trade_id"] for t in trades] == ["a", "b"]


def _position(spread_id: str = "IC_SPY-20260406T100000", status: str = "open"):
    return SpreadPosition(
        spread_id=spread_id,
        spread_type="iron_condor",
        underlying="SPY",
        entry_time="2026-04-06T10:00:00-04:00",
        expiration_date="2026-04-06",
        strategy="IC_SPY",
        net_credit=0.5,
        quantity=3,
        status=status,
    )


class TestConditionalWrites:
    def test_create_then_read_round_trips_integers(self, dynamo_tables):
        created = dynamo_io.create_position(dynamo_tables["positions"], _position())
        stored = dynamo_io.get_position(dynamo_tables["positions"], "IC_SPY", created.spread_id)
        assert stored.version == 1
        assert stored.quantity == 3
        assert isinstance(stored.quantity, int)

    def test_second_create_with_the_same_key_conflicts(self, dynamo_tables):
        dynamo_io.create_position(dynamo_tables["positions"], _position())
        with pytest.raises(dynamo_io.PositionConflictError):
            dynamo_io.create_position(dynamo_tables["positions"], _position())

    def test_stale_snapshot_cannot_overwrite_a_close(self, dynamo_tables):
        """A monitor holding an earlier open snapshot must not undo the closer's transition."""
        table = dynamo_tables["positions"]
        dynamo_io.create_position(table, _position())
        closer_view = dynamo_io.get_position(table, "IC_SPY", "IC_SPY-20260406T100000")
        monitor_view = dynamo_io.get_position(table, "IC_SPY", "IC_SPY-20260406T100000")

        dynamo_io.save_position(table, replace(closer_view, status="closing"))
        with pytest.raises(dynamo_io.PositionConflictError):
            dynamo_io.save_position(table, replace(monitor_view, current_value=0.2))

        stored = dynamo_io.get_position(table, "IC_SPY", "IC_SPY-20260406T100000")
        assert stored.status == "closing"
        assert stored.version == 2

    def test_active_positions_exclude_closed_and_failed(self, dynamo_tables):
        table = dynamo_tables["positions"]
        for spread_id, status in [
            ("a", "pending_open"),
            ("b", "open"),
            ("c", "closing"),
            ("d", "closed_profit"),
            ("e", "entry_failed"),
        ]:
            dynamo_io.create_position(table, _position(spread_id, status))
        active = dynamo_io.get_active_positions(table)
        assert sorted(p.spread_id for p in active) == ["a", "b", "c"]
