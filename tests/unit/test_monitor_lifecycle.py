"""Monitor and closer runs against moto DynamoDB and a broker fake returning alpaca-py models."""

from datetime import datetime, timedelta

import pytest

from closer import handler as closer
from common import orders
from common.dynamo_io import create_position, get_position, get_positions, get_trades_for_date
from common.models import SpreadLeg, SpreadPosition
from monitor import handler as monitor

from .broker_fakes import (
    FakeOptionsClient,
    FakeTradingClient,
    FastClock,
    make_clock,
    make_order,
    make_quote,
)

NOW = "2026-04-06T11:00:00-04:00"
SESSION_CLOSE = "2026-04-06T16:00:00-04:00"
SHORT_PUT = "SPY260406P00540000"
LONG_PUT = "SPY260406P00539000"
SPREAD_ID = "CS_SPY-20260406T100000"


def _position(**overrides) -> SpreadPosition:
    fields = {
        "spread_id": SPREAD_ID,
        "spread_type": "put_credit_spread",
        "underlying": "SPY",
        "entry_time": "2026-04-06T10:00:30-04:00",
        "expiration_date": "2026-04-06",
        "strategy": "CS_SPY",
        "net_credit": 0.50,
        "current_value": 0.50,
        "stop_loss_price": 1.00,
        "take_profit_price": 0.25,
        "quantity": 2,
        "status": "open",
        "status_time": "2026-04-06T10:00:30-04:00",
    }
    fields.update(overrides)
    position = SpreadPosition(**fields)
    position.set_legs(
        [
            SpreadLeg("l0", SHORT_PUT, "sell", "put", 540.0, 1, 0.70),
            SpreadLeg("l1", LONG_PUT, "buy", "put", 539.0, 1, 0.20),
        ]
    )
    return position


def _quotes(short_mid: float, long_mid: float, age=timedelta(seconds=10)) -> dict:
    stamp = datetime.fromisoformat(NOW) - age
    return {
        SHORT_PUT: make_quote(SHORT_PUT, short_mid - 0.01, short_mid + 0.01, stamp),
        LONG_PUT: make_quote(LONG_PUT, long_mid - 0.01, long_mid + 0.01, stamp),
    }


def _close_fill(debit_short: float, debit_long: float, filled_fraction: float = 1.0):
    def on_submit(request):
        qty = int(request.qty)
        filled = int(qty * filled_fraction)
        return (
            make_order(
                request.client_order_id,
                "filled" if filled == qty else "canceled",
                qty=qty,
                filled_qty=filled,
                limit_price=request.limit_price,
                leg_fills=[(SHORT_PUT, "buy", debit_short), (LONG_PUT, "sell", debit_long)],
            ),
            False,
        )

    return on_submit


@pytest.fixture
def broker(monkeypatch, dynamo_tables):
    monkeypatch.setattr(orders, "time", FastClock())
    trading = FakeTradingClient(make_clock(NOW, SESSION_CLOSE))
    options = FakeOptionsClient({})
    monkeypatch.setattr(monitor, "create_trading_client", lambda secret: trading)
    monkeypatch.setattr(monitor, "create_options_data_client", lambda secret: options)
    return trading, options


def _stored() -> SpreadPosition:
    return get_position("premium-positions", "CS_SPY", SPREAD_ID)


def _close_trades() -> list[dict]:
    today = datetime.now(monitor.ET).strftime("%Y-%m-%d")
    trades = get_trades_for_date("premium-trades", "CS_SPY", today)
    return [t for t in trades if t["action"] == "CLOSE"]


class TestQuoteValidation:
    def test_empty_quote_response_does_not_trigger_take_profit(self, broker):
        create_position("premium-positions", _position())
        result = monitor.process_active_positions(force_close=False)

        assert result["unpriced"] == 1
        assert broker[0].submitted == []
        assert _stored().status == "open"

    def test_missing_leg_quote_leaves_position_unpriced(self, broker):
        broker[1].quotes = {SHORT_PUT: _quotes(0.30, 0.10)[SHORT_PUT]}
        create_position("premium-positions", _position())
        assert monitor.process_active_positions(force_close=False)["unpriced"] == 1

    def test_stale_quote_leaves_position_unpriced(self, broker):
        broker[1].quotes = _quotes(0.10, 0.05, age=timedelta(minutes=10))
        create_position("premium-positions", _position())
        assert monitor.process_active_positions(force_close=False)["unpriced"] == 1

    def test_quotes_are_requested_for_the_stored_contracts(self, broker):
        broker[1].quotes = _quotes(0.40, 0.05)
        create_position("premium-positions", _position())
        monitor.process_active_positions(force_close=False)
        assert broker[1].requested == [[SHORT_PUT, LONG_PUT]]

    def test_priced_position_stores_dollar_unrealized_pnl(self, broker):
        broker[1].quotes = _quotes(0.40, 0.05)
        create_position("premium-positions", _position())
        assert monitor.process_active_positions(force_close=False)["open"] == 1
        stored = _stored()
        assert stored.current_value == pytest.approx(0.35)
        # (0.50 - 0.35) points x 2 spreads x 100
        assert stored.unrealized_pnl == pytest.approx(30.0)


class TestCloses:
    def test_filled_stop_records_realized_dollars(self, broker):
        broker[0].on_submit = _close_fill(1.30, 0.10)
        broker[1].quotes = _quotes(1.30, 0.10)
        create_position("premium-positions", _position())
        result = monitor.process_active_positions(force_close=False)

        assert result["closed"] == 1
        assert _stored().status == "closed_stop"
        (trade,) = _close_trades()
        # (0.50 credit - 1.20 debit) x 2 spreads x 100
        assert trade["realized_pnl"] == pytest.approx(-140.0)
        assert trade["quantity"] == 2

    def test_rejected_close_keeps_the_position_open(self, broker):
        broker[0].on_submit = lambda request: (
            make_order(request.client_order_id, "rejected", 2),
            False,
        )
        broker[1].quotes = _quotes(1.30, 0.10)
        create_position("premium-positions", _position())
        result = monitor.process_active_positions(force_close=False)

        assert result["open"] == 1
        assert _stored().status == "open"
        assert _close_trades() == []

    def test_partial_close_keeps_the_rest_open(self, broker):
        broker[0].on_submit = _close_fill(0.20, 0.05, filled_fraction=0.5)
        broker[1].quotes = _quotes(0.20, 0.05)
        create_position("premium-positions", _position())
        monitor.process_active_positions(force_close=False)

        stored = _stored()
        assert stored.status == "open"
        assert stored.quantity == 1
        (trade,) = _close_trades()
        assert trade["realized_pnl"] == pytest.approx(35.0)

    def test_unconfirmed_close_is_settled_by_a_later_run(self, broker, monkeypatch):
        trading, options = broker
        trading.on_submit = lambda request: (
            make_order(request.client_order_id, "new", 2, limit_price=request.limit_price),
            False,
        )
        options.quotes = _quotes(1.30, 0.10)
        create_position("premium-positions", _position())
        assert monitor.process_active_positions(force_close=False)["closing"] == 1

        closing = _stored()
        filled = make_order(
            closing.close_client_order_id,
            "filled",
            qty=2,
            filled_qty=2,
            leg_fills=[(SHORT_PUT, "buy", 1.30), (LONG_PUT, "sell", 0.10)],
        )
        trading.orders[closing.close_client_order_id] = filled.model_copy(
            update={"id": trading.orders[closing.close_client_order_id].id}
        )
        trading.clock = make_clock("2026-04-06T11:05:00-04:00", SESSION_CLOSE)
        monkeypatch.setattr(monitor, "_age", lambda position, now: timedelta(minutes=5))
        assert monitor.process_active_positions(force_close=False)["closed"] == 1
        assert _stored().status == "closed_stop"

    def test_stale_snapshot_does_not_close_twice(self, broker, monkeypatch):
        trading, options = broker
        trading.on_submit = _close_fill(1.30, 0.10)
        options.quotes = _quotes(1.30, 0.10)
        created = create_position("premium-positions", _position())
        tables = {"positions": "premium-positions", "trades": "premium-trades"}
        now = datetime.fromisoformat(NOW)

        assert monitor.process_position("s", tables, created, now, False) == "closed"
        assert monitor.process_position("s", tables, created, now, False) == "conflict"
        assert len(trading.submitted) == 1


class TestSession:
    def test_market_closed_skips_without_quotes(self, broker):
        broker[0].clock = make_clock(NOW, SESSION_CLOSE, is_open=False)
        create_position("premium-positions", _position())
        result = monitor.process_active_positions(force_close=False)
        assert result["skipped"] is True
        assert broker[1].requested == []

    def test_early_close_deadline_closes_positions(self, broker):
        trading, options = broker
        trading.clock = make_clock("2026-11-27T12:46:00-05:00", "2026-11-27T13:00:00-05:00")
        trading.on_submit = _close_fill(0.30, 0.05)
        stamp = datetime.fromisoformat("2026-11-27T12:45:50-05:00")
        short_put = "SPY261127P00540000"
        long_put = "SPY261127P00539000"
        options.quotes = {
            short_put: make_quote(short_put, 0.29, 0.31, stamp),
            long_put: make_quote(long_put, 0.04, 0.06, stamp),
        }
        position = _position(expiration_date="2026-11-27", status_time="")
        position.set_legs(
            [
                SpreadLeg("l0", short_put, "sell", "put", 540.0, 1, 0.70),
                SpreadLeg("l1", long_put, "buy", "put", 539.0, 1, 0.20),
            ]
        )
        create_position("premium-positions", position)
        assert monitor.process_active_positions(force_close=False)["closed"] == 1
        assert _stored().status == "closed_eod"

    def test_position_from_an_earlier_session_is_reported_not_requoted(self, broker):
        broker[0].clock = make_clock("2026-04-07T11:00:00-04:00", "2026-04-07T16:00:00-04:00")
        create_position("premium-positions", _position())
        result = monitor.process_active_positions(force_close=False)
        assert result["expired"] == 1
        assert broker[1].requested == []


class TestPendingEntries:
    def test_entry_filled_after_the_executor_returned_is_opened(self, broker, monkeypatch):
        trading, options = broker
        trading.orders[SPREAD_ID] = make_order(
            SPREAD_ID,
            "filled",
            qty=2,
            filled_qty=2,
            leg_fills=[(SHORT_PUT, "sell", 0.70), (LONG_PUT, "buy", 0.20)],
        )
        options.quotes = _quotes(0.45, 0.15)
        create_position("premium-positions", _position(status="pending_open"))
        result = monitor.process_active_positions(force_close=False)
        assert result["open"] == 1
        assert _stored().status == "open"

    def test_young_pending_entry_is_left_to_the_executor(self, broker):
        create_position(
            "premium-positions",
            _position(status="pending_open", status_time="2026-04-06T10:59:30-04:00"),
        )
        assert monitor.process_active_positions(force_close=False)["pending_open"] == 1

    def test_old_pending_entry_without_an_order_fails(self, broker):
        create_position("premium-positions", _position(status="pending_open"))
        assert monitor.process_active_positions(force_close=False)["entry_failed"] == 1


class TestCloser:
    def test_closer_closes_every_open_position(self, broker):
        trading, options = broker
        trading.on_submit = _close_fill(0.45, 0.10)
        options.quotes = _quotes(0.45, 0.10)
        create_position("premium-positions", _position())
        result = closer._close_all()
        assert result["positions_closed"] == 1
        assert result["positions_active"] == 0
        assert _stored().status == "closed_eod"

    def test_closer_reports_positions_left_when_the_market_is_closed(self, broker):
        broker[0].clock = make_clock(NOW, SESSION_CLOSE, is_open=False)
        create_position("premium-positions", _position())
        result = closer._close_all()
        assert result == {"positions_closed": 0, "positions_active": 1}

    def test_closer_without_positions(self, broker):
        assert closer._close_all()["positions_closed"] == 0
        assert get_positions("premium-positions", "CS_SPY") == {}
