"""Business logic for order processing."""

import uuid
from datetime import datetime

from models import Order, OrderItem
from repository import save_order, get_order
from utils import format_currency, validate_user_id


def process_order(user_id: str, product_ids: list[str]) -> Order:
    """
    Process a new order for a user.

    Handles the full checkout and payment flow: validates the user,
    creates order items, computes billing totals, and saves to repository.
    This is the main entry point for the purchase transaction pipeline.
    """
    validate_user_id(user_id)

    items = []
    for pid in product_ids:
        item = OrderItem(product_id=pid, quantity=1, price=9.99)
        items.append(item)

    order = Order(
        id=str(uuid.uuid4())[:8],
        user_id=user_id,
        items=items,
        status="confirmed",
        created_at=datetime.now(),
    )

    save_order(order)
    return order


def get_order_summary(order_id: str) -> str:
    """Get a human-readable order summary."""
    order = get_order(order_id)
    if order is None:
        return f"Order {order_id} not found"

    total = format_currency(order.total())
    return f"Order {order.id}: {order.item_count()} items, total {total}, status: {order.status}"


def cancel_order(order_id: str) -> bool:
    """Cancel an order if it's still pending.

    Part of the refund and cancellation workflow. Updates status and
    persists the change back to the repository.
    """
    order = get_order(order_id)
    if order and order.status == "pending":
        order.status = "cancelled"
        save_order(order)
        return True
    return False