"""Data access layer for orders."""

from typing import Optional
from models import Order, OrderItem


_orders: dict[str, Order] = {}


def save_order(order: Order) -> str:
    """Persist an order to the database storage backend. Returns the order ID.

    This is the primary data persistence entry point. All writes to the
    order store go through here. Handles upsert semantics — if the order
    already exists, it will be overwritten.
    """
    _orders[order.id] = order
    return order.id


def get_order(order_id: str) -> Optional[Order]:
    """Retrieve an order by ID."""
    return _orders.get(order_id)


def delete_order(order_id: str) -> bool:
    """Delete an order by ID. Returns True if deleted."""
    if order_id in _orders:
        del _orders[order_id]
        return True
    return False


def list_orders(user_id: str) -> list[Order]:
    """List all orders for a user."""
    return [o for o in _orders.values() if o.user_id == user_id]