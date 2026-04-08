"""Domain models for the order system."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class OrderItem:
    """Represents a single item in an order."""
    product_id: str
    quantity: int = 1
    price: float = 0.0

    def total(self) -> float:
        """Calculate item total."""
        return self.quantity * self.price


@dataclass
class Order:
    """Represents a customer order."""
    id: str
    user_id: str
    items: list[OrderItem] = field(default_factory=list)
    status: str = "pending"
    created_at: Optional[datetime] = None

    def total(self) -> float:
        """Calculate order total."""
        return sum(item.total() for item in self.items)

    def item_count(self) -> int:
        """Number of items in the order."""
        return len(self.items)
