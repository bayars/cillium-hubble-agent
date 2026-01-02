"""
Event Bus - In-memory pub/sub for real-time event distribution.

Provides:
- Event subscription for WebSocket clients
- Event publishing from agents
- Event history buffer
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from weakref import WeakSet

logger = logging.getLogger(__name__)


@dataclass
class Event:
    """Wrapper for events with metadata."""
    type: str
    data: dict
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }


class Subscriber:
    """Represents a subscriber to events."""

    def __init__(self, event_types: Optional[list[str]] = None):
        """
        Initialize subscriber.

        Args:
            event_types: List of event types to receive (None = all)
        """
        self.event_types = set(event_types) if event_types else None
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.created_at = datetime.now()
        self._active = True

    def should_receive(self, event_type: str) -> bool:
        """Check if subscriber should receive this event type."""
        if self.event_types is None:
            return True
        return event_type in self.event_types

    async def get_event(self, timeout: float = 30.0) -> Optional[Event]:
        """Get next event with timeout."""
        if not self._active:
            return None
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self):
        """Mark subscriber as inactive."""
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active


class EventBus:
    """
    Central event bus for distributing events to subscribers.

    Provides:
    - Subscribe/unsubscribe mechanism
    - Event publishing with type filtering
    - Event history buffer
    """

    def __init__(self, history_size: int = 100):
        """
        Initialize event bus.

        Args:
            history_size: Number of events to keep in history
        """
        self._subscribers: set[Subscriber] = set()
        self._history: deque[Event] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()
        self._event_count = 0

    async def subscribe(
        self,
        event_types: Optional[list[str]] = None
    ) -> Subscriber:
        """
        Create a new subscription.

        Args:
            event_types: List of event types to receive (None = all)

        Returns:
            Subscriber instance
        """
        subscriber = Subscriber(event_types)
        async with self._lock:
            self._subscribers.add(subscriber)

        logger.debug(f"New subscriber added (total: {len(self._subscribers)})")
        return subscriber

    async def unsubscribe(self, subscriber: Subscriber):
        """Remove a subscription."""
        subscriber.close()
        async with self._lock:
            self._subscribers.discard(subscriber)

        logger.debug(f"Subscriber removed (total: {len(self._subscribers)})")

    async def publish(
        self,
        event_type: str,
        data: dict,
        source: str = "unknown"
    ):
        """
        Publish an event to all matching subscribers.

        Args:
            event_type: Type of event (e.g., "link_state_change")
            data: Event data
            source: Event source identifier
        """
        event = Event(type=event_type, data=data, source=source)

        # Add to history
        self._history.append(event)
        self._event_count += 1

        # Distribute to subscribers
        dead_subscribers = []

        async with self._lock:
            for subscriber in self._subscribers:
                if not subscriber.is_active:
                    dead_subscribers.append(subscriber)
                    continue

                if subscriber.should_receive(event_type):
                    try:
                        subscriber.queue.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning("Subscriber queue full, dropping event")

            # Clean up dead subscribers
            for sub in dead_subscribers:
                self._subscribers.discard(sub)

        logger.debug(
            f"Published event '{event_type}' to "
            f"{len(self._subscribers)} subscribers"
        )

    def get_history(
        self,
        event_type: Optional[str] = None,
        limit: int = 100
    ) -> list[Event]:
        """
        Get recent events from history.

        Args:
            event_type: Filter by event type (None = all)
            limit: Maximum number of events to return

        Returns:
            List of recent events
        """
        events = list(self._history)

        if event_type:
            events = [e for e in events if e.type == event_type]

        return events[-limit:]

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    @property
    def event_count(self) -> int:
        """Total number of events published."""
        return self._event_count


# Global event bus instance
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def reset_event_bus():
    """Reset the global event bus (for testing)."""
    global _event_bus
    _event_bus = None
