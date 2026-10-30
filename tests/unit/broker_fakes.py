"""Offline broker stand-ins that return alpaca-py response models.

Orders, quotes, clocks and positions are built with the SDK's own pydantic
models, so field types and enums match what the handlers receive in production.
"""

import json
import uuid
from datetime import datetime

from alpaca.common.exceptions import APIError
from alpaca.data.models import Quote
from alpaca.trading.models import Clock, Order, Position, TradeAccount

TIMESTAMP = "2026-04-06T14:00:00Z"


class _HttpError:
    def __init__(self, status_code: int):
        self.response = type("Response", (), {"status_code": status_code})()
        self.request = None


def api_error(status_code: int, message: str) -> APIError:
    body = json.dumps({"code": status_code * 100, "message": message})
    return APIError(body, _HttpError(status_code))


def make_order(
    client_order_id: str,
    status: str,
    qty: int,
    filled_qty: int = 0,
    limit_price: float = -0.5,
    leg_fills: list[tuple[str, str, float | None]] | None = None,
) -> Order:
    """Build an MLEG order. leg_fills holds (symbol, side, filled_avg_price) per leg."""
    legs = [
        {
            "id": str(uuid.uuid4()),
            "client_order_id": f"{client_order_id}-leg{i}",
            "created_at": TIMESTAMP,
            "updated_at": TIMESTAMP,
            "submitted_at": TIMESTAMP,
            "asset_id": str(uuid.uuid4()),
            "symbol": symbol,
            "asset_class": "us_option",
            "qty": str(qty),
            "filled_qty": str(filled_qty),
            "filled_avg_price": None if price is None else str(price),
            "order_class": "mleg",
            "order_type": "limit",
            "type": "limit",
            "side": side,
            "time_in_force": "day",
            "status": status,
            "extended_hours": False,
            "ratio_qty": "1",
        }
        for i, (symbol, side, price) in enumerate(leg_fills or [])
    ]
    return Order(
        id=str(uuid.uuid4()),
        client_order_id=client_order_id,
        created_at=TIMESTAMP,
        updated_at=TIMESTAMP,
        submitted_at=TIMESTAMP,
        qty=str(qty),
        filled_qty=str(filled_qty),
        order_class="mleg",
        order_type="limit",
        type="limit",
        time_in_force="day",
        limit_price=str(limit_price),
        status=status,
        extended_hours=False,
        legs=legs or None,
    )


def make_clock(now: str, next_close: str, is_open: bool = True) -> Clock:
    return Clock(timestamp=now, is_open=is_open, next_open=next_close, next_close=next_close)


def make_quote(symbol: str, bid: float, ask: float, timestamp: datetime) -> Quote:
    raw = {"t": timestamp.isoformat(), "bp": bid, "bs": 1, "ap": ask, "as": 1, "c": []}
    return Quote(symbol, raw)


def make_broker_position(symbol: str, qty: int) -> Position:
    return Position(
        asset_id=str(uuid.uuid4()),
        symbol=symbol,
        exchange="",
        asset_class="us_option",
        avg_entry_price="0.5",
        qty=str(abs(qty)),
        side="long" if qty > 0 else "short",
        cost_basis="0",
    )


class FakeTradingClient:
    """In-memory broker. on_submit decides what a new order looks like.

    on_submit(request) returns (order, raise_after_accept). With raise_after_accept
    the order is stored and a timeout is raised, as when the response is lost.
    """

    def __init__(self, clock: Clock, on_submit=None, equity: float = 100_000.0):
        self.clock = clock
        self.on_submit = on_submit
        self.equity = equity
        self.orders: dict[str, Order] = {}
        self.submitted: list = []
        self.canceled: list[str] = []
        self.positions: list[Position] = []
        # Applied to an order when cancel is requested: client_order_id -> resulting order
        self.after_cancel: dict[str, Order] = {}

    def get_clock(self) -> Clock:
        return self.clock

    def get_account(self) -> TradeAccount:
        return TradeAccount(
            id=str(uuid.uuid4()),
            account_number="PA0000",
            status="ACTIVE",
            equity=str(self.equity),
        )

    def submit_order(self, request) -> Order:
        if request.client_order_id in self.orders:
            raise api_error(422, "client_order_id must be unique")
        self.submitted.append(request)
        order, raise_after_accept = self.on_submit(request)
        self.orders[request.client_order_id] = order
        if raise_after_accept:
            raise TimeoutError("read timed out")
        return order

    def get_order_by_client_id(self, client_order_id: str) -> Order:
        if client_order_id not in self.orders:
            raise api_error(404, "order not found")
        return self.orders[client_order_id]

    def get_order_by_id(self, order_id) -> Order:
        for order in self.orders.values():
            if order.id == order_id:
                return order
        raise api_error(404, "order not found")

    def cancel_order_by_id(self, order_id) -> None:
        order = self.get_order_by_id(order_id)
        self.canceled.append(order.client_order_id)
        replacement = self.after_cancel.get(order.client_order_id)
        if replacement is None:
            replacement = Order(**{**order.model_dump(), "status": "canceled"})
        self.orders[order.client_order_id] = Order(**{**replacement.model_dump(), "id": order.id})

    def get_all_positions(self) -> list[Position]:
        return self.positions

    def get_orders(self, filter=None) -> list[Order]:
        terminal = {"filled", "canceled", "expired", "rejected", "done_for_day"}
        return [o for o in self.orders.values() if o.status.value not in terminal]


class FakeOptionsClient:
    def __init__(self, quotes: dict[str, Quote]):
        self.quotes = quotes
        self.requested: list[list[str]] = []

    def get_option_latest_quote(self, request) -> dict[str, Quote]:
        symbols = list(request.symbol_or_symbols)
        self.requested.append(symbols)
        return {s: self.quotes[s] for s in symbols if s in self.quotes}


class FastClock:
    """Stand-in for the time module in common.orders: sleep advances monotonic time."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
