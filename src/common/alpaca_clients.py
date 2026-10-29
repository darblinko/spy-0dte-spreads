"""Factory for creating Alpaca API clients backed by AWS Secrets Manager."""

import json
import logging

import boto3
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.trading import TradingClient

logger = logging.getLogger(__name__)

_secrets_client = None
_secret_cache = {}


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def get_secret(secret_name: str) -> dict:
    """Retrieve API credentials from Secrets Manager. Cached per invocation."""
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]

    client = _get_secrets_client()
    response = client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    _secret_cache[secret_name] = secret
    return secret


def create_trading_client(secret_name: str) -> TradingClient:
    secret = get_secret(secret_name)
    return TradingClient(secret["api_key"], secret["api_secret"], paper=True)


def create_stock_data_client(secret_name: str) -> StockHistoricalDataClient:
    secret = get_secret(secret_name)
    return StockHistoricalDataClient(secret["api_key"], secret["api_secret"])


def create_options_data_client(secret_name: str) -> OptionHistoricalDataClient:
    secret = get_secret(secret_name)
    return OptionHistoricalDataClient(api_key=secret["api_key"], secret_key=secret["api_secret"])


def is_market_open(trading_client: TradingClient) -> tuple:
    """Check if market is open. Returns (is_open, clock) tuple."""
    clock = trading_client.get_clock()
    return clock.is_open, clock
