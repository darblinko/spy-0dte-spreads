"""Shared fixtures for unit tests."""

import boto3
import pytest
from moto import mock_aws

from common import dynamo_io, metrics

POSITIONS_TABLE = "premium-positions"
TRADES_TABLE = "premium-trades"


def _create_table(client, name: str, partition_key: str, sort_key: str) -> None:
    client.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": partition_key, "KeyType": "HASH"},
            {"AttributeName": sort_key, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": partition_key, "AttributeType": "S"},
            {"AttributeName": sort_key, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class _DiscardingCloudWatch:
    def put_metric_data(self, **kwargs) -> None:
        pass


@pytest.fixture(autouse=True)
def no_cloudwatch(monkeypatch):
    """Handlers publish dashboard metrics; unit tests never reach CloudWatch."""
    monkeypatch.setattr(metrics, "_cw_client", _DiscardingCloudWatch())


@pytest.fixture
def dynamo_tables(monkeypatch):
    """In-memory DynamoDB (moto) with the positions and trades tables of StorageStack."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("POSITIONS_TABLE", POSITIONS_TABLE)
    monkeypatch.setenv("TRADES_TABLE", TRADES_TABLE)
    with mock_aws():
        monkeypatch.setattr(dynamo_io, "_dynamodb", None)
        client = boto3.client("dynamodb")
        _create_table(client, POSITIONS_TABLE, "strategy", "spread_id")
        _create_table(client, TRADES_TABLE, "strategy", "trade_id")
        yield {"positions": POSITIONS_TABLE, "trades": TRADES_TABLE}
    dynamo_io._dynamodb = None
