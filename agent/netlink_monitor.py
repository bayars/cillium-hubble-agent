"""
Netlink Monitor - Real-time link state change detection via kernel netlink socket.

Provides instant detection of:
- Link up events
- Link down events
- Interface additions/removals
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Callable, Optional

try:
    from pyroute2 import IPRoute
    from pyroute2.netlink.rtnl import RTM_NEWLINK, RTM_DELLINK
    PYROUTE2_AVAILABLE = True
except ImportError:
    PYROUTE2_AVAILABLE = False

logger = logging.getLogger(__name__)


class LinkEvent(str, Enum):
    LINK_UP = "link_up"
    LINK_DOWN = "link_down"
    LINK_ADDED = "link_added"
    LINK_REMOVED = "link_removed"


@dataclass
class LinkStateChange:
    """Represents a link state change event."""
    interface: str
    ifindex: int
    event: LinkEvent
    timestamp: datetime
    operstate: str  # 'up', 'down', 'unknown'
    flags: int

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "ifindex": self.ifindex,
            "event": self.event.value,
            "timestamp": self.timestamp.isoformat(),
            "operstate": self.operstate,
            "flags": self.flags,
        }


# Operstate mapping from kernel values
OPERSTATE_MAP = {
    0: "unknown",
    1: "notpresent",
    2: "down",
    3: "lowerlayerdown",
    4: "testing",
    5: "dormant",
    6: "up",
}


class NetlinkMonitor:
    """
    Monitor network interface state changes via Netlink socket.

    Uses pyroute2 to subscribe to RTM_NEWLINK events for instant
    notification of link state changes.
    """

    def __init__(
        self,
        callback: Optional[Callable[[LinkStateChange], None]] = None,
        interface_filter: Optional[list[str]] = None,
    ):
        """
        Initialize the Netlink monitor.

        Args:
            callback: Optional callback function for state changes
            interface_filter: Optional list of interface names to monitor.
                             If None, monitors all interfaces except lo.
        """
        if not PYROUTE2_AVAILABLE:
            raise RuntimeError("pyroute2 is required for Netlink monitoring. Install with: pip install pyroute2")

        self.callback = callback
        self.interface_filter = interface_filter
        self._running = False
        self._ipr: Optional[IPRoute] = None
        self._event_queue: asyncio.Queue[LinkStateChange] = asyncio.Queue()
        self._previous_states: dict[str, str] = {}

    def _should_monitor(self, ifname: str) -> bool:
        """Check if interface should be monitored."""
        # Skip loopback
        if ifname == "lo":
            return False

        # If filter specified, only monitor listed interfaces
        if self.interface_filter:
            return ifname in self.interface_filter

        return True

    def _parse_message(self, msg) -> Optional[LinkStateChange]:
        """Parse a netlink message into a LinkStateChange event."""
        try:
            ifname = msg.get_attr("IFLA_IFNAME")
            if not ifname or not self._should_monitor(ifname):
                return None

            ifindex = msg.get("index", 0)
            flags = msg.get("flags", 0)
            operstate_val = msg.get_attr("IFLA_OPERSTATE")
            operstate = OPERSTATE_MAP.get(operstate_val, "unknown")

            # Determine event type based on state change
            prev_state = self._previous_states.get(ifname)

            if prev_state is None:
                # New interface
                event = LinkEvent.LINK_ADDED
            elif operstate == "up" and prev_state != "up":
                event = LinkEvent.LINK_UP
            elif operstate != "up" and prev_state == "up":
                event = LinkEvent.LINK_DOWN
            else:
                # State unchanged, skip
                return None

            self._previous_states[ifname] = operstate

            return LinkStateChange(
                interface=ifname,
                ifindex=ifindex,
                event=event,
                timestamp=datetime.now(),
                operstate=operstate,
                flags=flags,
            )
        except Exception as e:
            logger.error(f"Error parsing netlink message: {e}")
            return None

    async def _poll_netlink(self):
        """Poll netlink socket for events (runs in executor)."""
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # Use executor to avoid blocking
                msgs = await loop.run_in_executor(
                    None,
                    lambda: self._ipr.get() if self._ipr else []
                )

                for msg in msgs:
                    if msg.get("event") in ("RTM_NEWLINK", "RTM_DELLINK"):
                        change = self._parse_message(msg)
                        if change:
                            await self._event_queue.put(change)
                            if self.callback:
                                self.callback(change)

            except Exception as e:
                if self._running:
                    logger.error(f"Netlink poll error: {e}")
                    await asyncio.sleep(0.1)

    async def get_current_states(self) -> dict[str, str]:
        """Get current state of all interfaces."""
        loop = asyncio.get_event_loop()
        states = {}

        try:
            with IPRoute() as ipr:
                links = await loop.run_in_executor(None, ipr.get_links)
                for link in links:
                    ifname = link.get_attr("IFLA_IFNAME")
                    if ifname and self._should_monitor(ifname):
                        operstate_val = link.get_attr("IFLA_OPERSTATE")
                        states[ifname] = OPERSTATE_MAP.get(operstate_val, "unknown")
        except Exception as e:
            logger.error(f"Error getting interface states: {e}")

        return states

    async def start(self):
        """Start monitoring netlink events."""
        if self._running:
            return

        logger.info("Starting Netlink monitor...")

        # Get initial states
        self._previous_states = await self.get_current_states()
        logger.info(f"Initial interface states: {self._previous_states}")

        # Open netlink socket
        self._ipr = IPRoute()
        self._ipr.bind()
        self._running = True

        # Start polling task
        asyncio.create_task(self._poll_netlink())

        logger.info("Netlink monitor started")

    async def stop(self):
        """Stop monitoring."""
        logger.info("Stopping Netlink monitor...")
        self._running = False

        if self._ipr:
            self._ipr.close()
            self._ipr = None

        logger.info("Netlink monitor stopped")

    async def events(self) -> AsyncIterator[LinkStateChange]:
        """Async iterator for link state change events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    @property
    def is_running(self) -> bool:
        return self._running


# Standalone usage example
async def main():
    """Example usage of NetlinkMonitor."""
    def on_change(event: LinkStateChange):
        print(f"[{event.timestamp}] {event.interface}: {event.event.value} (operstate: {event.operstate})")

    monitor = NetlinkMonitor(callback=on_change)

    try:
        await monitor.start()
        print("Monitoring link state changes. Press Ctrl+C to stop.")

        async for event in monitor.events():
            print(f"Event: {event.to_dict()}")

    except KeyboardInterrupt:
        pass
    finally:
        await monitor.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
