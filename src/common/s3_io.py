"""S3 I/O for JSON and parquet files."""

import io
import json
import logging

import boto3
import pandas as pd

logger = logging.getLogger(__name__)

_s3_client = None


def _get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client('s3')
    return _s3_client


def read_parquet_from_s3(bucket: str, key: str) -> pd.DataFrame:
    s3 = _get_s3()
    response = s3.get_object(Bucket=bucket, Key=key)
    return pd.read_parquet(io.BytesIO(response['Body'].read()))


def write_parquet_to_s3(df: pd.DataFrame, bucket: str, key: str) -> None:
    s3 = _get_s3()
    buffer = io.BytesIO()
    df.to_parquet(buffer)
    buffer.seek(0)
    s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
    logger.info(f"Wrote parquet to s3://{bucket}/{key} ({len(df)} rows)")


def write_json_to_s3(bucket: str, key: str, data: dict) -> None:
    """Write JSON data to S3."""
    s3 = _get_s3()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(data, default=str),
        ContentType='application/json',
    )
    logger.info(f"Wrote JSON to s3://{bucket}/{key}")


def read_json_from_s3(bucket: str, key: str) -> dict:
    """Read JSON data from S3."""
    s3 = _get_s3()
    response = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(response['Body'].read().decode('utf-8'))
