"""Event models and serializers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Event:
    """Core event model."""
    id: str
    title: str
    description: str
    date: datetime
    venue: Optional[str] = None
    capacity: int = 100


class EventSerializer:
    """Serializer for the Event model."""

    def __init__(self, data=None, many=False):
        self.data = data
        self.many = many

    def is_valid(self, raise_exception=False):
        return True

    def save(self):
        return self.data
