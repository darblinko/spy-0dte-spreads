"""Dashboard metrics: published by the handlers, never allowed to fail a run."""

import pytest

from common import metrics
from monitor import handler as monitor
from reconciler import handler as reconciler

from .test_monitor_lifecycle import _position


class _RecordingCloudWatch:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def put_metric_data(self, **kwargs):
        if self.fail:
            raise ConnectionError("cloudwatch unavailable")
        self.calls.append(kwargs)


@pytest.fixture
def cloudwatch(monkeypatch):
    client = _RecordingCloudWatch()
    monkeypatch.setattr(metrics, "_cw_client", client)
    return client


def _published(client) -> dict:
    result = {}
    for call in client.calls:
        assert call["Namespace"] == "Premium/Trading"
        for datum in call["MetricData"]:
            dims = tuple((d["Name"], d["Value"]) for d in datum.get("Dimensions", []))
            result[(datum["MetricName"], dims)] = datum["Value"]
    return result


def test_publish_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setattr(metrics, "_cw_client", _RecordingCloudWatch(fail=True))
    metrics.try_publish_metric("DailyPnL", 1.0)
    assert "Failed to publish metric DailyPnL" in caplog.text


def test_monitor_publishes_active_positions_per_strategy(cloudwatch, dynamo_tables):
    from common.dynamo_io import create_position

    create_position(dynamo_tables["positions"], _position(status="pending_open"))
    monitor._publish_active_positions(monitor.get_active_positions(dynamo_tables["positions"]))

    published = _published(cloudwatch)
    assert published[("ActivePositions", (("Strategy", "CS_SPY"),))] == 1
    assert published[("ActivePositions", (("Strategy", "IC_SPY"),))] == 0


def test_reconciler_publishes_daily_pnl_and_win_rate(cloudwatch):
    reconciler._publish_daily_metrics(
        {"total_realized_pnl_usd": -35.0, "win_rate": 0.5, "trades_closed": 2}
    )
    published = _published(cloudwatch)
    assert published[("DailyPnL", ())] == -35.0
    assert published[("WinRate", ())] == 0.5


def test_reconciler_skips_win_rate_without_closed_trades(cloudwatch):
    reconciler._publish_daily_metrics(
        {"total_realized_pnl_usd": 0.0, "win_rate": 0.0, "trades_closed": 0}
    )
    assert ("WinRate", ()) not in _published(cloudwatch)
