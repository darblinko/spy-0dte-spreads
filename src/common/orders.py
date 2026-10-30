"""Broker order helpers: multi-leg spread requests, recovery by client order ID, fill states.

Every order carries a deterministic client order ID. The broker rejects a second
order with the same ID, so a retried submission either finds the first order or
places the only one.
"""

import logging
import time
from dataclasses import dataclass

from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from .models import SpreadPosition

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
        OrderStatus.DONE_FOR_DAY,
    }
)
POLL_SECONDS = 2.0
HTTP_NOT_FOUND = 404


@dataclass(frozen=True)
class FillState:
    """What an order has done so far.

    net_credit is per spread, sells minus buys: positive for a credit received,
    negative for a debit paid. It comes from the leg fill prices, or from the
    limit price when the broker reports no leg prices.
    """

    filled_qty: int
    terminal: bool
    net_credit: float


def build_spread_request(
    position: SpreadPosition, opening: bool, limit_price: float, client_order_id: str
) -> LimitOrderRequest:
    """Multi-leg limit order for position.quantity spreads.

    limit_price follows the broker convention: negative for a net credit,
    positive for a net debit. Closing reverses each leg.
    """
    option_legs = []
    for leg in position.get_legs():
        if opening:
            sold = PositionIntent.SELL_TO_OPEN
            bought = PositionIntent.BUY_TO_OPEN
        else:
            sold = PositionIntent.BUY_TO_CLOSE
            bought = PositionIntent.SELL_TO_CLOSE
        option_legs.append(
            OptionLegRequest(
                symbol=leg.contract_id,
                ratio_qty=leg.quantity,
                position_intent=sold if leg.side == "sell" else bought,
            )
        )
    return LimitOrderRequest(
        order_class=OrderClass.MLEG,
        qty=position.quantity,
        limit_price=round(limit_price, 2),
        time_in_force=TimeInForce.DAY,
        legs=option_legs,
        client_order_id=client_order_id,
    )


def find_order_by_client_id(trading_client, client_order_id: str):
    """Return the broker order with this client order ID, or None if there is none."""
    try:
        return trading_client.get_order_by_client_id(client_order_id)
    except APIError as e:
        if e.status_code == HTTP_NOT_FOUND:
            return None
        raise


def submit_or_recover(trading_client, request: LimitOrderRequest):
    """Submit the order unless the broker already has one with its client order ID.

    If submission raises, for example on a timeout after the broker accepted the
    order, the existing order is looked up before the error is re-raised.
    """
    existing = find_order_by_client_id(trading_client, request.client_order_id)
    if existing is not None:
        logger.info(f"Recovered existing order {request.client_order_id}")
        return existing
    try:
        order = trading_client.submit_order(request)
    except Exception:
        existing = find_order_by_client_id(trading_client, request.client_order_id)
        if existing is None:
            raise
        logger.warning(f"Submission raised but the broker has {request.client_order_id}")
        return existing
    logger.info(f"Submitted order {request.client_order_id} id={order.id}")
    return order


def is_terminal(order) -> bool:
    return order.status in TERMINAL_STATUSES


def wait_for_terminal(trading_client, order, timeout_seconds: float):
    """Poll the order until it reaches a terminal status or the timeout passes."""
    deadline = time.monotonic() + timeout_seconds
    while not is_terminal(order) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        order = trading_client.get_order_by_id(order.id)
    return order


def cancel_and_refresh(trading_client, order, timeout_seconds: float):
    """Request cancellation of a working order and return its state afterwards."""
    if is_terminal(order):
        return order
    try:
        trading_client.cancel_order_by_id(order.id)
    except APIError as e:
        # The broker refuses to cancel an order that filled in the meantime.
        logger.warning(f"Cancel of {order.client_order_id} refused: {e}")
    order = trading_client.get_order_by_id(order.id)
    return wait_for_terminal(trading_client, order, timeout_seconds)


def fill_state(order) -> FillState:
    filled_qty = int(float(order.filled_qty or 0))
    return FillState(
        filled_qty=filled_qty,
        terminal=is_terminal(order),
        net_credit=_net_credit(order),
    )


def _net_credit(order) -> float:
    legs = order.legs or []
    if legs and all(leg.filled_avg_price is not None for leg in legs):
        total = 0.0
        for leg in legs:
            price = float(leg.filled_avg_price) * int(float(leg.ratio_qty or 1))
            total += price if leg.side == OrderSide.SELL else -price
        return round(total, 4)
    logger.warning(f"No leg fill prices on {order.client_order_id}; using the limit price")
    return -float(order.limit_price or 0.0)
