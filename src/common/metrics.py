"""CloudWatch custom metrics helpers."""

import logging

import boto3

logger = logging.getLogger(__name__)

NAMESPACE = "Premium/Trading"
_cw_client = None


def _get_cw():
    global _cw_client
    if _cw_client is None:
        _cw_client = boto3.client("cloudwatch")
    return _cw_client


def publish_metric(
    metric_name: str, value: float, unit: str = "Count", dimensions: dict | None = None
) -> None:
    cw = _get_cw()
    metric_data = {
        "MetricName": metric_name,
        "Value": value,
        "Unit": unit,
    }
    if dimensions:
        metric_data["Dimensions"] = [{"Name": k, "Value": str(v)} for k, v in dimensions.items()]

    cw.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[metric_data],
    )


def try_publish_metric(
    metric_name: str, value: float, unit: str = "Count", dimensions: dict | None = None
) -> None:
    """Publish a dashboard metric, logging instead of raising: metrics never stop a run."""
    try:
        publish_metric(metric_name, value, unit, dimensions)
    except Exception:
        logger.warning(f"Failed to publish metric {metric_name}", exc_info=True)
