"""Centralized configuration for premium-selling strategies."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PremiumConfig:
    name: str  # "IC_SPY" or "CS_SPY"
    underlying: str  # "SPY"
    secret_name: str  # Secrets Manager secret name

    # Strike selection
    short_delta_min: float  # Minimum delta for short strike
    short_delta_max: float  # Maximum delta for short strike
    spread_width: float  # Points between short and long strikes
    min_credit: float  # Minimum acceptable credit per spread

    # Risk management
    stop_loss_multiplier: float  # Close at N * credit received
    take_profit_pct: float  # Take profit at this % of credit
    max_positions: int  # Max concurrent spreads
    max_daily_loss: float  # Dollar stop for the day
    position_size_pct: float  # % of equity per spread

    # Timing (all ET)
    earliest_entry: str  # "10:00"
    latest_entry: str  # "14:30"
    force_close_time: str  # "15:45"
    regime_detection_time: str  # "10:00"

    # Regime detection thresholds
    vwap_cross_threshold: int  # >= N crosses in 30 min = range
    candle_body_trend: float  # Body ratio above this = trend signal
    candle_body_range: float  # Body ratio below this = range signal
    regime_confidence_min: float  # Minimum confidence to trade


# Iron condor config: used on range days
IC_SPY_CONFIG = PremiumConfig(
    name="IC_SPY",
    underlying="SPY",
    secret_name="premium/spy-api",
    short_delta_min=0.10,
    short_delta_max=0.20,
    spread_width=1.0,
    min_credit=0.15,
    stop_loss_multiplier=2.0,
    take_profit_pct=0.50,
    max_positions=3,
    max_daily_loss=500.0,
    position_size_pct=0.05,
    earliest_entry="10:00",
    latest_entry="14:30",
    force_close_time="15:45",
    regime_detection_time="10:00",
    vwap_cross_threshold=3,
    candle_body_trend=0.6,
    candle_body_range=0.4,
    regime_confidence_min=0.6,
)

# Credit spread config: used on trend days
CS_SPY_CONFIG = PremiumConfig(
    name="CS_SPY",
    underlying="SPY",
    secret_name="premium/spy-api",
    short_delta_min=0.15,
    short_delta_max=0.20,
    spread_width=1.0,
    min_credit=0.15,
    stop_loss_multiplier=2.0,
    take_profit_pct=0.50,
    max_positions=3,
    max_daily_loss=500.0,
    position_size_pct=0.05,
    earliest_entry="10:00",
    latest_entry="14:30",
    force_close_time="15:45",
    regime_detection_time="10:00",
    vwap_cross_threshold=3,
    candle_body_trend=0.6,
    candle_body_range=0.4,
    regime_confidence_min=0.6,
)

CONFIGS = {"IC_SPY": IC_SPY_CONFIG, "CS_SPY": CS_SPY_CONFIG}


def get_config(strategy: str) -> PremiumConfig:
    if strategy not in CONFIGS:
        raise ValueError(f"Unknown strategy: {strategy}. Must be one of {list(CONFIGS.keys())}")
    return CONFIGS[strategy]


def get_s3_bucket() -> str:
    return os.environ["S3_BUCKET"]


def get_positions_table() -> str:
    return os.environ["POSITIONS_TABLE"]


def get_trades_table() -> str:
    return os.environ["TRADES_TABLE"]


def get_market_state_table() -> str:
    return os.environ["MARKET_STATE_TABLE"]


def get_sns_trade_topic() -> str:
    return os.environ["SNS_TRADE_TOPIC"]


def get_sns_error_topic() -> str:
    return os.environ["SNS_ERROR_TOPIC"]
