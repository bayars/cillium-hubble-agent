"""
Event Publisher - Publishes interface state changes to API/message queue.

Supports multiple publishing targets:
- HTTP POST to API server
- WebSocket to API server
- Redis Pub/Sub (optional, for horizontal scaling)
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class PublishResult:
    """Result of a publish operation."""
    success: bool
    target: str
    message: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class Publisher(ABC):
    """Abstract base class for event publishers."""

    @abstractmethod
    async def connect(self):
        """Connect to the target."""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect from the target."""
        pass

    @abstractmethod
    async def publish(self, event: dict) -> PublishResult:
        """Publish an event."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected."""
        pass


class HttpPublisher(Publisher):
    """Publishes events via HTTP POST to an API endpoint."""

    def __init__(
        self,
        api_url: str,
        timeout: float = 5.0,
        retry_count: int = 3,
        retry_delay: float = 1.0,
    ):
        """
        Initialize HTTP publisher.

        Args:
            api_url: URL to POST events to
            timeout: Request timeout in seconds
            retry_count: Number of retries on failure
            retry_delay: Delay between retries in seconds
        """
        self.api_url = api_url
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self._session: Optional[aiohttp.ClientSession] = None
        self._connected = False

    async def connect(self):
        """Create HTTP session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            self._connected = True
            logger.info(f"HTTP publisher connected to {self.api_url}")

    async def disconnect(self):
        """Close HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._connected = False
            logger.info("HTTP publisher disconnected")

    async def publish(self, event: dict) -> PublishResult:
        """Publish event via HTTP POST."""
        if not self._session or self._session.closed:
            await self.connect()

        for attempt in range(self.retry_count):
            try:
                async with self._session.post(
                    self.api_url,
                    json=event,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status < 300:
                        return PublishResult(
                            success=True,
                            target=self.api_url,
                            message=f"HTTP {response.status}",
                        )
                    else:
                        return PublishResult(
                            success=False,
                            target=self.api_url,
                            message=f"HTTP {response.status}: {await response.text()}",
                        )

            except asyncio.TimeoutError:
                logger.warning(f"HTTP publish timeout (attempt {attempt + 1}/{self.retry_count})")
            except aiohttp.ClientError as e:
                logger.warning(f"HTTP publish error (attempt {attempt + 1}/{self.retry_count}): {e}")

            if attempt < self.retry_count - 1:
                await asyncio.sleep(self.retry_delay)

        return PublishResult(
            success=False,
            target=self.api_url,
            message=f"Failed after {self.retry_count} attempts",
        )

    @property
    def is_connected(self) -> bool:
        return self._connected and self._session is not None and not self._session.closed


class WebSocketPublisher(Publisher):
    """Publishes events via WebSocket connection."""

    def __init__(
        self,
        ws_url: str,
        reconnect_delay: float = 5.0,
    ):
        """
        Initialize WebSocket publisher.

        Args:
            ws_url: WebSocket URL to connect to
            reconnect_delay: Delay before reconnecting on disconnect
        """
        self.ws_url = ws_url
        self.reconnect_delay = reconnect_delay
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._connected = False

    async def connect(self):
        """Establish WebSocket connection."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(self.ws_url)
            self._connected = True
            logger.info(f"WebSocket publisher connected to {self.ws_url}")
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self._connected = False
            raise

    async def disconnect(self):
        """Close WebSocket connection."""
        if self._ws and not self._ws.closed:
            await self._ws.close()

        if self._session and not self._session.closed:
            await self._session.close()

        self._connected = False
        logger.info("WebSocket publisher disconnected")

    async def publish(self, event: dict) -> PublishResult:
        """Publish event via WebSocket."""
        if not self._connected or self._ws is None or self._ws.closed:
            try:
                await self.connect()
            except Exception as e:
                return PublishResult(
                    success=False,
                    target=self.ws_url,
                    message=f"Connection failed: {e}",
                )

        try:
            await self._ws.send_json(event)
            return PublishResult(
                success=True,
                target=self.ws_url,
                message="Sent via WebSocket",
            )
        except Exception as e:
            self._connected = False
            return PublishResult(
                success=False,
                target=self.ws_url,
                message=f"Send failed: {e}",
            )

    @property
    def is_connected(self) -> bool:
        return self._connected and self._ws is not None and not self._ws.closed


class InMemoryPublisher(Publisher):
    """In-memory publisher for local event handling (no network)."""

    def __init__(self, queue: Optional[asyncio.Queue] = None):
        """
        Initialize in-memory publisher.

        Args:
            queue: Optional queue to publish to. If None, creates internal queue.
        """
        self._queue = queue or asyncio.Queue()
        self._connected = False

    async def connect(self):
        """Mark as connected."""
        self._connected = True
        logger.info("In-memory publisher connected")

    async def disconnect(self):
        """Mark as disconnected."""
        self._connected = False
        logger.info("In-memory publisher disconnected")

    async def publish(self, event: dict) -> PublishResult:
        """Publish to in-memory queue."""
        try:
            await self._queue.put(event)
            return PublishResult(
                success=True,
                target="in-memory",
                message="Added to queue",
            )
        except Exception as e:
            return PublishResult(
                success=False,
                target="in-memory",
                message=str(e),
            )

    @property
    def queue(self) -> asyncio.Queue:
        """Get the event queue."""
        return self._queue

    @property
    def is_connected(self) -> bool:
        return self._connected


class MultiPublisher(Publisher):
    """Publishes to multiple targets."""

    def __init__(self, publishers: list[Publisher]):
        """
        Initialize multi-publisher.

        Args:
            publishers: List of publishers to use
        """
        self.publishers = publishers

    async def connect(self):
        """Connect all publishers."""
        for pub in self.publishers:
            try:
                await pub.connect()
            except Exception as e:
                logger.error(f"Failed to connect {type(pub).__name__}: {e}")

    async def disconnect(self):
        """Disconnect all publishers."""
        for pub in self.publishers:
            try:
                await pub.disconnect()
            except Exception as e:
                logger.error(f"Failed to disconnect {type(pub).__name__}: {e}")

    async def publish(self, event: dict) -> PublishResult:
        """Publish to all targets."""
        results = await asyncio.gather(
            *[pub.publish(event) for pub in self.publishers],
            return_exceptions=True
        )

        success_count = sum(
            1 for r in results
            if isinstance(r, PublishResult) and r.success
        )

        return PublishResult(
            success=success_count > 0,
            target="multi",
            message=f"{success_count}/{len(self.publishers)} succeeded",
        )

    @property
    def is_connected(self) -> bool:
        return any(pub.is_connected for pub in self.publishers)


class EventPublisher:
    """
    High-level event publisher with buffering and batching.

    Wraps underlying publishers and provides:
    - Event buffering during disconnection
    - Batch publishing option
    - Automatic reconnection
    """

    def __init__(
        self,
        publisher: Publisher,
        buffer_size: int = 1000,
        batch_size: int = 1,
        batch_interval: float = 0.0,
    ):
        """
        Initialize event publisher.

        Args:
            publisher: Underlying publisher implementation
            buffer_size: Max events to buffer during disconnection
            batch_size: Number of events to batch (1 = no batching)
            batch_interval: Max time to wait for batch (0 = no waiting)
        """
        self.publisher = publisher
        self.buffer_size = buffer_size
        self.batch_size = batch_size
        self.batch_interval = batch_interval

        self._buffer: list[dict] = []
        self._running = False
        self._publish_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the publisher."""
        await self.publisher.connect()
        self._running = True
        logger.info("Event publisher started")

    async def stop(self):
        """Stop the publisher and flush buffer."""
        self._running = False

        # Flush remaining events
        if self._buffer:
            await self._flush_buffer()

        await self.publisher.disconnect()
        logger.info("Event publisher stopped")

    async def publish(self, event: dict) -> PublishResult:
        """Publish an event."""
        if self.batch_size <= 1:
            # No batching, publish immediately
            result = await self.publisher.publish(event)
            if not result.success:
                self._buffer_event(event)
            return result
        else:
            # Buffer for batching
            self._buffer_event(event)
            if len(self._buffer) >= self.batch_size:
                await self._flush_buffer()
            return PublishResult(success=True, target="buffered", message="Queued for batch")

    def _buffer_event(self, event: dict):
        """Add event to buffer."""
        if len(self._buffer) >= self.buffer_size:
            # Drop oldest event
            self._buffer.pop(0)
            logger.warning("Event buffer full, dropping oldest event")

        self._buffer.append(event)

    async def _flush_buffer(self):
        """Publish all buffered events."""
        while self._buffer:
            event = self._buffer.pop(0)
            result = await self.publisher.publish(event)
            if not result.success:
                # Put back at front and stop
                self._buffer.insert(0, event)
                logger.warning("Failed to flush buffer, will retry later")
                break

    @property
    def is_connected(self) -> bool:
        return self.publisher.is_connected

    @property
    def buffer_count(self) -> int:
        return len(self._buffer)


# Factory function
def create_publisher(
    publisher_type: str = "memory",
    **kwargs
) -> Publisher:
    """
    Create a publisher instance.

    Args:
        publisher_type: One of "http", "websocket", "memory", "multi"
        **kwargs: Arguments passed to publisher constructor

    Returns:
        Publisher instance
    """
    if publisher_type == "http":
        return HttpPublisher(**kwargs)
    elif publisher_type == "websocket":
        return WebSocketPublisher(**kwargs)
    elif publisher_type == "memory":
        return InMemoryPublisher(**kwargs)
    else:
        raise ValueError(f"Unknown publisher type: {publisher_type}")
