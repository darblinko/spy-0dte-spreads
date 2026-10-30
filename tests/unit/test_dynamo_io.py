"""Tests for DynamoDB access."""

from decimal import Decimal

from common import dynamo_io


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
