"""SNS notification helpers."""

import json
import logging

import boto3

logger = logging.getLogger(__name__)

_sns_client = None


def _get_sns():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


def publish_trade_alert(
    topic_arn: str,
    strategy: str,
    action: str,
    spread_id: str,
    spread_type: str,
    net_credit: float,
    underlying: str,
) -> None:
    message = {
        "strategy": strategy,
        "action": action,
        "spread_id": spread_id,
        "spread_type": spread_type,
        "net_credit": net_credit,
        "underlying": underlying,
    }
    sns = _get_sns()
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"Premium {strategy}: {action} {spread_type}",
        Message=json.dumps(message, default=str),
    )
    logger.info(f"Published trade alert: {action} {spread_type} ({spread_id})")


def publish_error_alert(topic_arn: str, source: str, error: str, details: str = "") -> None:
    message = {
        "source": source,
        "error": error,
        "details": details,
    }
    sns = _get_sns()
    sns.publish(
        TopicArn=topic_arn,
        Subject=f"Premium ERROR: {source}",
        Message=json.dumps(message, default=str),
    )
    logger.error(f"Published error alert: {source} - {error}")
