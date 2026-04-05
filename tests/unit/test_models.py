"""Tests for premium-selling data models."""

import json
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from common.models import (
    SpreadLeg,
    SpreadPosition,
    TradeRecord,
    MarketRegime,
    SpreadType,
    PositionStatus,
    generate_spread_id,
)


class TestSpreadLeg:
    def test_create_leg(self):
        leg = SpreadLeg(
            leg_id="order-123",
            contract_id="SPY260404P00520000",
            side="sell",
            option_type="put",
            strike=520.0,
            quantity=1,
            fill_price=0.85,
            current_price=0.50,
            delta_at_entry=-0.15,
        )
        assert leg.strike == 520.0
        assert leg.side == "sell"

    def test_to_dict(self):
        leg = SpreadLeg(
            leg_id="order-123",
            contract_id="SPY260404P00520000",
            side="sell",
            option_type="put",
            strike=520.0,
            quantity=1,
            fill_price=0.85,
        )
        d = leg.to_dict()
        assert d["leg_id"] == "order-123"
        assert d["strike"] == 520.0
        assert d["current_price"] == 0.0

    def test_from_dict(self):
        data = {
            "leg_id": "order-456",
            "contract_id": "SPY260404C00530000",
            "side": "buy",
            "option_type": "call",
            "strike": 530.0,
            "quantity": 1,
            "fill_price": 0.25,
            "current_price": 0.10,
            "delta_at_entry": 0.12,
        }
        leg = SpreadLeg.from_dict(data)
        assert leg.leg_id == "order-456"
        assert leg.strike == 530.0
        assert leg.delta_at_entry == 0.12

    def test_from_dict_ignores_extra_fields(self):
        data = {
            "leg_id": "order-789",
            "contract_id": "SPY260404P00510000",
            "side": "sell",
            "option_type": "put",
            "strike": 510.0,
            "quantity": 1,
            "fill_price": 0.90,
            "extra_field": "ignored",
        }
        leg = SpreadLeg.from_dict(data)
        assert leg.leg_id == "order-789"
        assert not hasattr(leg, "extra_field")


class TestSpreadPosition:
    def _make_position(self) -> SpreadPosition:
        legs = [
            SpreadLeg("o1", "SPY260404P00518000", "sell", "put", 518.0, 1, 0.85, 0.50, -0.15),
            SpreadLeg("o2", "SPY260404P00517000", "buy", "put", 517.0, 1, 0.25, 0.10, -0.08),
        ]
        pos = SpreadPosition(
            spread_id="abc123",
            spread_type=SpreadType.PUT_CREDIT_SPREAD.value,
            underlying="SPY",
            entry_time="2026-04-04T10:30:00",
            expiration_date="2026-04-04",
            strategy="CS_SPY",
            net_credit=0.60,
            current_value=0.40,
            unrealized_pnl=0.20,
            max_loss=0.40,
            stop_loss_price=1.20,
            take_profit_price=0.30,
            status=PositionStatus.OPEN.value,
            market_regime=MarketRegime.UPTREND.value,
            vix_at_entry=18.5,
            spy_price_at_entry=525.0,
            expected_move_at_entry=3.5,
        )
        pos.set_legs(legs)
        return pos

    def test_create_position(self):
        pos = self._make_position()
        assert pos.spread_id == "abc123"
        assert pos.spread_type == "put_credit_spread"
        assert pos.net_credit == 0.60
        assert len(pos.legs) == 2

    def test_get_legs(self):
        pos = self._make_position()
        legs = pos.get_legs()
        assert len(legs) == 2
        assert isinstance(legs[0], SpreadLeg)
        assert legs[0].strike == 518.0
        assert legs[1].strike == 517.0

    def test_set_legs(self):
        pos = SpreadPosition(
            spread_id="def456",
            spread_type=SpreadType.IRON_CONDOR.value,
            underlying="SPY",
            entry_time="2026-04-04T11:00:00",
            expiration_date="2026-04-04",
            strategy="IC_SPY",
        )
        legs = [
            SpreadLeg("o1", "SPY260404P00515000", "sell", "put", 515.0, 1, 0.80, 0.0, -0.12),
            SpreadLeg("o2", "SPY260404P00514000", "buy", "put", 514.0, 1, 0.20, 0.0, -0.06),
            SpreadLeg("o3", "SPY260404C00535000", "sell", "call", 535.0, 1, 0.75, 0.0, 0.12),
            SpreadLeg("o4", "SPY260404C00536000", "buy", "call", 536.0, 1, 0.18, 0.0, 0.06),
        ]
        pos.set_legs(legs)
        assert len(pos.legs) == 4
        recovered = pos.get_legs()
        assert recovered[2].strike == 535.0
        assert recovered[2].side == "sell"

    def test_dynamo_roundtrip(self):
        pos = self._make_position()
        item = pos.to_dynamo_item()

        # Verify Decimal conversion
        assert isinstance(item['net_credit'], Decimal)
        assert isinstance(item['vix_at_entry'], Decimal)
        assert item['spread_id'] == "abc123"

        # Legs stored as JSON string
        assert isinstance(item['legs'], str)
        legs_data = json.loads(item['legs'])
        assert len(legs_data) == 2

        # Roundtrip
        restored = SpreadPosition.from_dynamo_item(item)
        assert restored.spread_id == pos.spread_id
        assert restored.net_credit == pytest.approx(pos.net_credit, abs=1e-5)
        assert restored.vix_at_entry == pytest.approx(pos.vix_at_entry, abs=1e-5)
        assert len(restored.legs) == 2

    def test_from_dynamo_missing_fields(self):
        """Missing optional fields should use defaults."""
        item = {
            'spread_id': 'xyz789',
            'spread_type': 'iron_condor',
            'underlying': 'SPY',
            'entry_time': '2026-04-04T10:00:00',
            'expiration_date': '2026-04-04',
            'strategy': 'IC_SPY',
        }
        pos = SpreadPosition.from_dynamo_item(item)
        assert pos.spread_id == 'xyz789'
        assert pos.net_credit == 0.0
        assert pos.status == PositionStatus.OPEN.value


class TestTradeRecord:
    def test_create_trade(self):
        trade = TradeRecord(
            trade_date="2026-04-04",
            spread_id="abc123",
            strategy="CS_SPY",
            action="OPEN",
            spread_type="put_credit_spread",
            underlying="SPY",
            net_credit=0.60,
            market_regime="uptrend",
            vix=18.5,
            spy_price=525.0,
        )
        assert trade.action == "OPEN"
        assert trade.net_credit == 0.60

    def test_to_dynamo_item(self):
        trade = TradeRecord(
            trade_date="2026-04-04",
            spread_id="abc123",
            strategy="CS_SPY",
            action="CLOSE",
            spread_type="put_credit_spread",
            underlying="SPY",
            net_credit=0.60,
            close_value=0.30,
            realized_pnl=0.30,
            close_reason="profit",
        )
        item = trade.to_dynamo_item()
        assert isinstance(item['net_credit'], Decimal)
        assert isinstance(item['realized_pnl'], Decimal)
        assert item['close_reason'] == "profit"

    def test_empty_strings_skipped(self):
        trade = TradeRecord(
            trade_date="2026-04-04",
            spread_id="abc123",
            strategy="IC_SPY",
            action="OPEN",
            spread_type="iron_condor",
            underlying="SPY",
        )
        item = trade.to_dynamo_item()
        assert 'close_reason' not in item
        assert 'legs' not in item


class TestEnums:
    def test_market_regime_values(self):
        assert MarketRegime.RANGE.value == "range"
        assert MarketRegime.UPTREND.value == "uptrend"
        assert MarketRegime.DOWNTREND.value == "downtrend"
        assert MarketRegime.UNCERTAIN.value == "uncertain"

    def test_spread_type_values(self):
        assert SpreadType.IRON_CONDOR.value == "iron_condor"
        assert SpreadType.PUT_CREDIT_SPREAD.value == "put_credit_spread"
        assert SpreadType.CALL_CREDIT_SPREAD.value == "call_credit_spread"

    def test_position_status_values(self):
        assert PositionStatus.OPEN.value == "open"
        assert PositionStatus.CLOSED_PROFIT.value == "closed_profit"
        assert PositionStatus.CLOSED_STOP.value == "closed_stop"
        assert PositionStatus.CLOSED_EOD.value == "closed_eod"


class TestGenerateSpreadId:
    def test_returns_string(self):
        sid = generate_spread_id()
        assert isinstance(sid, str)

    def test_length(self):
        sid = generate_spread_id()
        assert len(sid) == 12

    def test_unique(self):
        ids = {generate_spread_id() for _ in range(100)}
        assert len(ids) == 100
