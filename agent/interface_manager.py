"""
Interface Manager - Unified interface state management.

Combines Netlink and sysfs monitoring to provide:
- Complete interface state (up/down, active/idle)
- Interface discovery and filtering
- Aggregated state for API consumption
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from .netlink_monitor import NetlinkMonitor, LinkStateChange, LinkEvent
from .sysfs_poller import SysfsPoller, TrafficStateChange, TrafficState, TrafficMetrics

logger = logging.getLogger(__name__)


class LinkState(str, Enum):
    """Combined link state."""
    UP_ACTIVE = "active"    # Link up, traffic flowing
    UP_IDLE = "idle"        # Link up, no traffic
    DOWN = "down"           # Link down
    UNKNOWN = "unknown"     # State not yet determined


@dataclass
class InterfaceInfo:
    """Complete information about a network interface."""
    name: str
    ifindex: int = 0
    mac_address: str = ""
    mtu: int = 1500
    speed_mbps: int = 0
    link_state: LinkState = LinkState.UNKNOWN
    operstate: str = "unknown"
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_bytes_total: int = 0
    tx_bytes_total: int = 0
    utilization: float = 0.0
    last_updated: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ifindex": self.ifindex,
            "mac_address": self.mac_address,
            "mtu": self.mtu,
            "speed_mbps": self.speed_mbps,
            "link_state": self.link_state.value,
            "operstate": self.operstate,
            "rx_bps": round(self.rx_bps, 2),
            "tx_bps": round(self.tx_bps, 2),
            "rx_bytes_total": self.rx_bytes_total,
            "tx_bytes_total": self.tx_bytes_total,
            "utilization": round(self.utilization, 4),
            "last_updated": self.last_updated.isoformat(),
        }


@dataclass
class StateChangeEvent:
    """Unified state change event."""
    interface: str
    old_state: LinkState
    new_state: LinkState
    interface_info: InterfaceInfo
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "unknown"  # "netlink" or "sysfs"

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "old_state": self.old_state.value,
            "new_state": self.new_state.value,
            "interface_info": self.interface_info.to_dict(),
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
        }


class InterfaceManager:
    """
    Manages interface state by combining Netlink and sysfs monitoring.

    Provides a unified view of interface state:
    - Link status from Netlink (instant up/down)
    - Traffic status from sysfs polling (active/idle)
    """

    SYSFS_NET_PATH = Path("/sys/class/net")

    # Default interfaces to exclude from monitoring
    DEFAULT_EXCLUDE = {"lo", "docker0", "br-", "veth", "virbr"}

    def __init__(
        self,
        poll_interval_ms: int = 100,
        interface_filter: Optional[list[str]] = None,
        exclude_patterns: Optional[set[str]] = None,
        state_change_callback: Optional[Callable[[StateChangeEvent], None]] = None,
    ):
        """
        Initialize the interface manager.

        Args:
            poll_interval_ms: Traffic polling interval in milliseconds
            interface_filter: If set, only monitor these interfaces
            exclude_patterns: Interface name patterns to exclude
            state_change_callback: Callback for state changes
        """
        self.interface_filter = interface_filter
        self.exclude_patterns = exclude_patterns or self.DEFAULT_EXCLUDE
        self.state_change_callback = state_change_callback

        self._interfaces: dict[str, InterfaceInfo] = {}
        self._running = False
        self._event_queue: asyncio.Queue[StateChangeEvent] = asyncio.Queue()

        # Initialize monitors with our filter
        monitored = self._discover_interfaces() if not interface_filter else interface_filter

        self._netlink_monitor = NetlinkMonitor(
            callback=self._on_link_change,
            interface_filter=monitored if interface_filter else None,
        )

        self._sysfs_poller = SysfsPoller(
            poll_interval_ms=poll_interval_ms,
            callback=self._on_traffic_change,
            interface_filter=monitored if interface_filter else None,
        )

    def _should_monitor(self, ifname: str) -> bool:
        """Check if interface should be monitored."""
        # Check explicit filter first
        if self.interface_filter:
            return ifname in self.interface_filter

        # Check exclusion patterns
        for pattern in self.exclude_patterns:
            if ifname.startswith(pattern) or ifname == pattern:
                return False

        return True

    def _discover_interfaces(self) -> list[str]:
        """Discover all monitorable interfaces."""
        interfaces = []
        try:
            for path in self.SYSFS_NET_PATH.iterdir():
                if path.is_dir() and self._should_monitor(path.name):
                    interfaces.append(path.name)
        except Exception as e:
            logger.error(f"Error discovering interfaces: {e}")
        return interfaces

    def _read_interface_info(self, ifname: str) -> InterfaceInfo:
        """Read static interface information from sysfs."""
        base = self.SYSFS_NET_PATH / ifname
        info = InterfaceInfo(name=ifname)

        try:
            # Read ifindex
            ifindex_path = base / "ifindex"
            if ifindex_path.exists():
                info.ifindex = int(ifindex_path.read_text().strip())

            # Read MAC address
            mac_path = base / "address"
            if mac_path.exists():
                info.mac_address = mac_path.read_text().strip()

            # Read MTU
            mtu_path = base / "mtu"
            if mtu_path.exists():
                info.mtu = int(mtu_path.read_text().strip())

            # Read speed
            speed_path = base / "speed"
            if speed_path.exists():
                try:
                    speed = int(speed_path.read_text().strip())
                    info.speed_mbps = max(speed, 0)
                except ValueError:
                    pass

            # Read operstate
            operstate_path = base / "operstate"
            if operstate_path.exists():
                info.operstate = operstate_path.read_text().strip()
                if info.operstate == "up":
                    info.link_state = LinkState.UP_IDLE
                else:
                    info.link_state = LinkState.DOWN

        except Exception as e:
            logger.error(f"Error reading interface info for {ifname}: {e}")

        return info

    def _compute_link_state(self, operstate: str, traffic_state: TrafficState) -> LinkState:
        """Compute combined link state from operstate and traffic state."""
        if operstate != "up":
            return LinkState.DOWN

        if traffic_state == TrafficState.ACTIVE:
            return LinkState.UP_ACTIVE
        elif traffic_state == TrafficState.IDLE:
            return LinkState.UP_IDLE
        else:
            return LinkState.UP_IDLE  # Default to idle if unknown

    def _on_link_change(self, event: LinkStateChange):
        """Handle link state change from Netlink."""
        ifname = event.interface

        if ifname not in self._interfaces:
            self._interfaces[ifname] = self._read_interface_info(ifname)

        info = self._interfaces[ifname]
        old_state = info.link_state

        # Update operstate
        info.operstate = event.operstate

        # Compute new state
        traffic_state = self._sysfs_poller.get_state(ifname)
        new_state = self._compute_link_state(event.operstate, traffic_state)

        if old_state != new_state:
            info.link_state = new_state
            info.last_updated = datetime.now()

            change = StateChangeEvent(
                interface=ifname,
                old_state=old_state,
                new_state=new_state,
                interface_info=info,
                source="netlink",
            )

            asyncio.create_task(self._emit_event(change))

    def _on_traffic_change(self, event: TrafficStateChange):
        """Handle traffic state change from sysfs poller."""
        ifname = event.interface

        if ifname not in self._interfaces:
            self._interfaces[ifname] = self._read_interface_info(ifname)

        info = self._interfaces[ifname]
        old_state = info.link_state

        # Update metrics
        info.rx_bps = event.metrics.rx_bps
        info.tx_bps = event.metrics.tx_bps
        info.rx_bytes_total = event.metrics.total_rx_bytes
        info.tx_bytes_total = event.metrics.total_tx_bytes
        info.utilization = event.metrics.utilization

        # Compute new state
        new_state = self._compute_link_state(info.operstate, event.new_state)

        if old_state != new_state:
            info.link_state = new_state
            info.last_updated = datetime.now()

            change = StateChangeEvent(
                interface=ifname,
                old_state=old_state,
                new_state=new_state,
                interface_info=info,
                source="sysfs",
            )

            asyncio.create_task(self._emit_event(change))

    async def _emit_event(self, event: StateChangeEvent):
        """Emit a state change event."""
        await self._event_queue.put(event)
        if self.state_change_callback:
            self.state_change_callback(event)
        logger.info(f"State change: {event.interface} {event.old_state.value} -> {event.new_state.value}")

    async def start(self):
        """Start monitoring all interfaces."""
        if self._running:
            return

        logger.info("Starting Interface Manager...")

        # Discover and initialize interfaces
        interfaces = self._discover_interfaces()
        for ifname in interfaces:
            self._interfaces[ifname] = self._read_interface_info(ifname)

        logger.info(f"Managing {len(self._interfaces)} interfaces: {list(self._interfaces.keys())}")

        # Start monitors
        await self._netlink_monitor.start()
        await self._sysfs_poller.start()

        self._running = True
        logger.info("Interface Manager started")

    async def stop(self):
        """Stop monitoring."""
        logger.info("Stopping Interface Manager...")
        self._running = False

        await self._netlink_monitor.stop()
        await self._sysfs_poller.stop()

        logger.info("Interface Manager stopped")

    async def events(self):
        """Async iterator for state change events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    def get_interface(self, ifname: str) -> Optional[InterfaceInfo]:
        """Get information for a specific interface."""
        info = self._interfaces.get(ifname)
        if info:
            # Update with latest metrics
            metrics = self._sysfs_poller.get_metrics(ifname)
            if metrics:
                info.rx_bps = metrics.rx_bps
                info.tx_bps = metrics.tx_bps
                info.rx_bytes_total = metrics.total_rx_bytes
                info.tx_bytes_total = metrics.total_tx_bytes
                info.utilization = metrics.utilization
        return info

    def get_all_interfaces(self) -> dict[str, InterfaceInfo]:
        """Get information for all interfaces."""
        result = {}
        for ifname in self._interfaces:
            info = self.get_interface(ifname)
            if info:
                result[ifname] = info
        return result

    def get_interface_states(self) -> dict[str, LinkState]:
        """Get state for all interfaces."""
        return {name: info.link_state for name, info in self._interfaces.items()}

    @property
    def is_running(self) -> bool:
        return self._running


# Standalone usage example
async def main():
    """Example usage of InterfaceManager."""
    def on_change(event: StateChangeEvent):
        print(f"\n[STATE CHANGE] {event.interface}: {event.old_state.value} -> {event.new_state.value}")
        info = event.interface_info
        print(f"  RX: {info.rx_bps:.0f} B/s, TX: {info.tx_bps:.0f} B/s")

    manager = InterfaceManager(
        poll_interval_ms=100,
        state_change_callback=on_change,
    )

    try:
        await manager.start()
        print("Monitoring interfaces. Press Ctrl+C to stop.\n")

        # Print status every 2 seconds
        while True:
            await asyncio.sleep(2)
            print("\n--- Current Interface States ---")
            for ifname, info in manager.get_all_interfaces().items():
                print(f"{ifname}: state={info.link_state.value}, "
                      f"rx={info.rx_bps:.0f} B/s, tx={info.tx_bps:.0f} B/s, "
                      f"util={info.utilization*100:.1f}%")

    except KeyboardInterrupt:
        pass
    finally:
        await manager.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
