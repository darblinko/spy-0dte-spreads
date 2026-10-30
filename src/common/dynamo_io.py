"""DynamoDB operations for spread positions, trades, and market state."""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .models import ACTIVE_STATUSES, SpreadPosition, TradeRecord

logger = logging.getLogger(__name__)

_dynamodb = None

STRATEGIES = ("IC_SPY", "CS_SPY")


class PositionConflictError(Exception):
    """The stored position was created or changed by another writer."""


def _get_table(table_name: str):
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(table_name)


def _sanitize_item(item: dict) -> dict:
    """Replace float values with Decimal for DynamoDB compatibility."""
    sanitized = {}
    for k, v in item.items():
        if isinstance(v, float):
            sanitized[k] = Decimal(str(round(v, 6)))
        elif v is None or v == "":
            continue
        else:
            sanitized[k] = v
    return sanitized


def _query_all(table, **query_kwargs) -> list[dict]:
    """Run a query and follow LastEvaluatedKey until every page is read."""
    items = []
    while True:
        response = table.query(**query_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if last_key is None:
            return items
        query_kwargs = {**query_kwargs, "ExclusiveStartKey": last_key}


# --- Positions ---


def get_positions(table_name: str, strategy: str) -> dict[str, SpreadPosition]:
    """Get every stored position for a strategy, in any status. Returns dict keyed by spread_id."""
    table = _get_table(table_name)
    items = _query_all(table, KeyConditionExpression=Key("strategy").eq(strategy))
    positions = {}
    for item in items:
        pos = SpreadPosition.from_dynamo_item(item)
        positions[pos.spread_id] = pos
    return positions


def get_position(table_name: str, strategy: str, spread_id: str) -> SpreadPosition | None:
    """Read one position with a strongly consistent read."""
    table = _get_table(table_name)
    response = table.get_item(
        Key={"strategy": strategy, "spread_id": spread_id}, ConsistentRead=True
    )
    item = response.get("Item")
    return SpreadPosition.from_dynamo_item(item) if item else None


def create_position(table_name: str, position: SpreadPosition) -> SpreadPosition:
    """Insert a new position at version 1.

    Raises PositionConflictError if a position with the same key exists.
    """
    created = replace(position, version=1)
    condition = Attr("spread_id").not_exists()
    _conditional_put(table_name, created, condition)
    logger.info(f"Created position: {created.strategy}/{created.spread_id}")
    return created


def save_position(table_name: str, position: SpreadPosition) -> SpreadPosition:
    """Write a position only if the stored version equals position.version.

    Returns the written position with its version incremented. Raises
    PositionConflictError if another writer changed the position since it
    was read, so a stale snapshot never overwrites a newer state.
    """
    saved = replace(position, version=position.version + 1)
    condition = Attr("version").eq(position.version)
    _conditional_put(table_name, saved, condition)
    logger.info(f"Saved position: {saved.strategy}/{saved.spread_id} v{saved.version}")
    return saved


def _conditional_put(table_name: str, position: SpreadPosition, condition) -> None:
    table = _get_table(table_name)
    item = position.to_dynamo_item()
    item["strategy"] = position.strategy
    item["updated_at"] = datetime.now(UTC).isoformat()
    try:
        table.put_item(Item=_sanitize_item(item), ConditionExpression=condition)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise PositionConflictError(f"{position.strategy}/{position.spread_id}") from e
        raise


def get_active_positions(table_name: str, strategies: tuple[str, ...] = STRATEGIES) -> list:
    """Get positions that may hold contracts or a working order, across strategies."""
    positions = []
    for strategy in strategies:
        stored = get_positions(table_name, strategy).values()
        positions.extend(p for p in stored if p.status in ACTIVE_STATUSES)
    return positions


def put_position(table_name: str, position: SpreadPosition) -> None:
    """Upsert a spread position."""
    table = _get_table(table_name)
    item = position.to_dynamo_item()
    item["strategy"] = position.strategy
    item["updated_at"] = datetime.now(UTC).isoformat()
    table.put_item(Item=_sanitize_item(item))
    logger.info(f"Saved position: {position.strategy}/{position.spread_id}")


def delete_position(table_name: str, strategy: str, spread_id: str) -> None:
    """Delete a closed position."""
    table = _get_table(table_name)
    table.delete_item(Key={"strategy": strategy, "spread_id": spread_id})
    logger.info(f"Deleted position: {strategy}/{spread_id}")


def get_all_open_positions(table_name: str) -> list[SpreadPosition]:
    """Get all open positions across all strategies."""
    positions = []
    for strategy in STRATEGIES:
        strategy_positions = get_positions(table_name, strategy)
        positions.extend(p for p in strategy_positions.values() if p.status == "open")
    return positions


# --- Trades ---


def record_trade(table_name: str, trade: TradeRecord) -> None:
    """Record a trade entry or exit."""
    table = _get_table(table_name)
    trade_id = f"{trade.trade_date}#{trade.spread_id}#{trade.action}"
    item = trade.to_dynamo_item()
    item["strategy"] = trade.strategy
    item["trade_id"] = trade_id
    table.put_item(Item=_sanitize_item(item))
    logger.info(f"Recorded trade: {trade.strategy}/{trade_id}")


def get_trades_for_date(table_name: str, strategy: str, trade_date: str) -> list[dict]:
    """Get all trades for a strategy on a given date."""
    table = _get_table(table_name)
    items = _query_all(
        table,
        KeyConditionExpression=(
            Key("strategy").eq(strategy) & Key("trade_id").begins_with(trade_date)
        ),
    )
    trades = []
    for item in items:
        trade = {k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()}
        trades.append(trade)
    return trades


# --- Market State ---


def put_market_state(table_name: str, date_str: str, state: dict) -> None:
    """Store market regime state for a given timestamp."""
    table = _get_table(table_name)
    now = datetime.now(UTC)
    item = {
        "date": date_str,
        "timestamp": now.isoformat(),
        "ttl_epoch": int(now.timestamp()) + (30 * 86400),
    }
    for k, v in state.items():
        if v is None:
            continue
        if isinstance(v, float):
            item[k] = Decimal(str(round(v, 6)))
        else:
            item[k] = str(v)
    table.put_item(Item=_sanitize_item(item))
    logger.info(f"Stored market state for {date_str}")


def get_latest_market_state(table_name: str, date_str: str) -> dict | None:
    """Get the most recent market state for today."""
    table = _get_table(table_name)
    response = table.query(
        KeyConditionExpression=Key("date").eq(date_str),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    item = items[0]
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()}
