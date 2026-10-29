"""Tests for premium-selling configuration."""

import os
import sys
from dataclasses import FrozenInstanceError

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from common.config import (
    CONFIGS,
    CS_SPY_CONFIG,
    IC_SPY_CONFIG,
    get_config,
)


class TestPremiumConfig:
    def test_ic_spy_config_exists(self):
        assert IC_SPY_CONFIG.name == "IC_SPY"
        assert IC_SPY_CONFIG.underlying == "SPY"

    def test_cs_spy_config_exists(self):
        assert CS_SPY_CONFIG.name == "CS_SPY"
        assert CS_SPY_CONFIG.underlying == "SPY"

    def test_config_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            IC_SPY_CONFIG.name = "CHANGED"

    def test_ic_delta_range(self):
        assert IC_SPY_CONFIG.short_delta_min == 0.10
        assert IC_SPY_CONFIG.short_delta_max == 0.20

    def test_cs_delta_range(self):
        assert CS_SPY_CONFIG.short_delta_min == 0.15
        assert CS_SPY_CONFIG.short_delta_max == 0.20

    def test_spread_width(self):
        assert IC_SPY_CONFIG.spread_width == 1.0
        assert CS_SPY_CONFIG.spread_width == 1.0

    def test_risk_management_defaults(self):
        for config in CONFIGS.values():
            assert config.stop_loss_multiplier == 2.0
            assert config.take_profit_pct == 0.50
            assert config.max_positions == 3
            assert config.min_credit == 0.15

    def test_timing_defaults(self):
        for config in CONFIGS.values():
            assert config.force_close_time == "15:45"
            assert config.earliest_entry == "10:00"
            assert config.regime_detection_time == "10:00"

    def test_regime_thresholds(self):
        for config in CONFIGS.values():
            assert config.vwap_cross_threshold == 3
            assert config.candle_body_trend == 0.6
            assert config.candle_body_range == 0.4
            assert config.regime_confidence_min == 0.6

    def test_shared_secret(self):
        assert IC_SPY_CONFIG.secret_name == CS_SPY_CONFIG.secret_name == "premium/spy-api"


class TestGetConfig:
    def test_get_ic_config(self):
        config = get_config("IC_SPY")
        assert config is IC_SPY_CONFIG

    def test_get_cs_config(self):
        config = get_config("CS_SPY")
        assert config is CS_SPY_CONFIG

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            get_config("INVALID")


class TestEnvironmentGetters:
    def test_get_s3_bucket(self):
        os.environ["S3_BUCKET"] = "test-bucket"
        from common.config import get_s3_bucket

        assert get_s3_bucket() == "test-bucket"
        del os.environ["S3_BUCKET"]

    def test_get_positions_table(self):
        os.environ["POSITIONS_TABLE"] = "test-positions"
        from common.config import get_positions_table

        assert get_positions_table() == "test-positions"
        del os.environ["POSITIONS_TABLE"]

    def test_get_trades_table(self):
        os.environ["TRADES_TABLE"] = "test-trades"
        from common.config import get_trades_table

        assert get_trades_table() == "test-trades"
        del os.environ["TRADES_TABLE"]

    def test_get_market_state_table(self):
        os.environ["MARKET_STATE_TABLE"] = "test-state"
        from common.config import get_market_state_table

        assert get_market_state_table() == "test-state"
        del os.environ["MARKET_STATE_TABLE"]

    def test_missing_env_var_raises(self):
        from common.config import get_s3_bucket

        if "S3_BUCKET" in os.environ:
            del os.environ["S3_BUCKET"]
        with pytest.raises(KeyError):
            get_s3_bucket()
