"""
sysfs Poller - Traffic statistics monitoring via /sys/class/net.

Polls interface statistics at configurable intervals to detect:
- Active traffic (bytes flowing)
- Idle state (no traffic)
- Traffic rates (bytes per second)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


class TrafficState(str, Enum):
    ACTIVE = "active"  # Traffic flowing
    IDLE = "idle"      # No traffic
    UNKNOWN = "unknown"


@dataclass
class InterfaceStats:
    """Raw statistics from sysfs."""
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class TrafficMetrics:
    """Computed traffic metrics."""
    interface: str
    rx_bps: float = 0.0  # Bytes per second
    tx_bps: float = 0.0
    rx_pps: float = 0.0  # Packets per second
    tx_pps: float = 0.0
    total_rx_bytes: int = 0
    total_tx_bytes: int = 0
    state: TrafficState = TrafficState.UNKNOWN
    utilization: float = 0.0  # 0.0 to 1.0, requires speed info
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "rx_bps": round(self.rx_bps, 2),
            "tx_bps": round(self.tx_bps, 2),
            "rx_pps": round(self.rx_pps, 2),
            "tx_pps": round(self.tx_pps, 2),
            "total_rx_bytes": self.total_rx_bytes,
            "total_tx_bytes": self.total_tx_bytes,
            "state": self.state.value,
            "utilization": round(self.utilization, 4),
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class TrafficStateChange:
    """Represents a traffic state change event."""
    interface: str
    old_state: TrafficState
    new_state: TrafficState
    metrics: TrafficMetrics
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "interface": self.interface,
            "old_state": self.old_state.value,
            "new_state": self.new_state.value,
            "metrics": self.metrics.to_dict(),
            "timestamp": self.timestamp.isoformat(),
        }


class SysfsPoller:
    """
    Poll /sys/class/net for interface statistics.

    Detects traffic activity and computes rates by comparing
    consecutive samples.
    """

    SYSFS_NET_PATH = Path("/sys/class/net")

    # Minimum bytes delta to consider as "active" traffic
    ACTIVE_THRESHOLD_BYTES = 0

    def __init__(
        self,
        poll_interval_ms: int = 100,
        callback: Optional[Callable[[TrafficStateChange], None]] = None,
        interface_filter: Optional[list[str]] = None,
        idle_threshold_samples: int = 5,  # Samples with no traffic before marking idle
    ):
        """
        Initialize the sysfs poller.

        Args:
            poll_interval_ms: Polling interval in milliseconds (default 100ms)
            callback: Optional callback for state changes
            interface_filter: Optional list of interfaces to monitor
            idle_threshold_samples: Number of consecutive zero-traffic samples
                                   before marking interface as idle
        """
        self.poll_interval = poll_interval_ms / 1000.0  # Convert to seconds
        self.callback = callback
        self.interface_filter = interface_filter
        self.idle_threshold = idle_threshold_samples

        self._running = False
        self._previous_stats: dict[str, InterfaceStats] = {}
        self._current_states: dict[str, TrafficState] = {}
        self._idle_counters: dict[str, int] = {}  # Count of idle samples
        self._event_queue: asyncio.Queue[TrafficStateChange] = asyncio.Queue()
        self._current_metrics: dict[str, TrafficMetrics] = {}

    def _should_monitor(self, ifname: str) -> bool:
        """Check if interface should be monitored."""
        if ifname == "lo":
            return False

        if self.interface_filter:
            return ifname in self.interface_filter

        return True

    def _get_interfaces(self) -> list[str]:
        """Get list of network interfaces from sysfs."""
        interfaces = []
        try:
            for path in self.SYSFS_NET_PATH.iterdir():
                if path.is_dir() and self._should_monitor(path.name):
                    interfaces.append(path.name)
        except Exception as e:
            logger.error(f"Error listing interfaces: {e}")
        return interfaces

    def _read_stat(self, ifname: str, stat_name: str) -> int:
        """Read a single statistic from sysfs."""
        try:
            path = self.SYSFS_NET_PATH / ifname / "statistics" / stat_name
            return int(path.read_text().strip())
        except (FileNotFoundError, ValueError, PermissionError):
            return 0

    def _read_stats(self, ifname: str) -> InterfaceStats:
        """Read all statistics for an interface."""
        return InterfaceStats(
            rx_bytes=self._read_stat(ifname, "rx_bytes"),
            tx_bytes=self._read_stat(ifname, "tx_bytes"),
            rx_packets=self._read_stat(ifname, "rx_packets"),
            tx_packets=self._read_stat(ifname, "tx_packets"),
            rx_errors=self._read_stat(ifname, "rx_errors"),
            tx_errors=self._read_stat(ifname, "tx_errors"),
            rx_dropped=self._read_stat(ifname, "rx_dropped"),
            tx_dropped=self._read_stat(ifname, "tx_dropped"),
            timestamp=datetime.now(),
        )

    def _read_speed(self, ifname: str) -> int:
        """Read interface speed in Mbps."""
        try:
            path = self.SYSFS_NET_PATH / ifname / "speed"
            speed = int(path.read_text().strip())
            return max(speed, 0)  # Speed can be -1 if unknown
        except (FileNotFoundError, ValueError, PermissionError):
            return 0

    def _compute_metrics(
        self,
        ifname: str,
        prev: InterfaceStats,
        curr: InterfaceStats
    ) -> TrafficMetrics:
        """Compute traffic metrics from two stat samples."""
        time_delta = (curr.timestamp - prev.timestamp).total_seconds()
        if time_delta <= 0:
            time_delta = self.poll_interval

        rx_bytes_delta = curr.rx_bytes - prev.rx_bytes
        tx_bytes_delta = curr.tx_bytes - prev.tx_bytes
        rx_packets_delta = curr.rx_packets - prev.rx_packets
        tx_packets_delta = curr.tx_packets - prev.tx_packets

        # Calculate rates
        rx_bps = rx_bytes_delta / time_delta
        tx_bps = tx_bytes_delta / time_delta
        rx_pps = rx_packets_delta / time_delta
        tx_pps = tx_packets_delta / time_delta

        # Calculate utilization if speed is known
        speed_mbps = self._read_speed(ifname)
        utilization = 0.0
        if speed_mbps > 0:
            speed_bps = speed_mbps * 1_000_000 / 8  # Convert Mbps to Bytes/s
            max_rate = max(rx_bps, tx_bps)
            utilization = min(max_rate / speed_bps, 1.0)

        # Determine traffic state
        has_traffic = (rx_bytes_delta > self.ACTIVE_THRESHOLD_BYTES or
                      tx_bytes_delta > self.ACTIVE_THRESHOLD_BYTES)

        if has_traffic:
            state = TrafficState.ACTIVE
            self._idle_counters[ifname] = 0
        else:
            self._idle_counters[ifname] = self._idle_counters.get(ifname, 0) + 1
            if self._idle_counters[ifname] >= self.idle_threshold:
                state = TrafficState.IDLE
            else:
                # Not enough samples yet to confirm idle
                state = self._current_states.get(ifname, TrafficState.UNKNOWN)

        return TrafficMetrics(
            interface=ifname,
            rx_bps=rx_bps,
            tx_bps=tx_bps,
            rx_pps=rx_pps,
            tx_pps=tx_pps,
            total_rx_bytes=curr.rx_bytes,
            total_tx_bytes=curr.tx_bytes,
            state=state,
            utilization=utilization,
            timestamp=curr.timestamp,
        )

    async def _poll_once(self):
        """Perform one poll cycle."""
        interfaces = self._get_interfaces()

        for ifname in interfaces:
            curr_stats = self._read_stats(ifname)
            prev_stats = self._previous_stats.get(ifname)

            if prev_stats:
                metrics = self._compute_metrics(ifname, prev_stats, curr_stats)
                self._current_metrics[ifname] = metrics

                # Check for state change
                old_state = self._current_states.get(ifname, TrafficState.UNKNOWN)
                new_state = metrics.state

                if old_state != new_state:
                    self._current_states[ifname] = new_state
                    change = TrafficStateChange(
                        interface=ifname,
                        old_state=old_state,
                        new_state=new_state,
                        metrics=metrics,
                    )
                    await self._event_queue.put(change)
                    if self.callback:
                        self.callback(change)
                    logger.info(f"Traffic state change: {ifname} {old_state.value} -> {new_state.value}")

            self._previous_stats[ifname] = curr_stats

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_once()
                await asyncio.sleep(self.poll_interval)
            except Exception as e:
                logger.error(f"Poll error: {e}")
                await asyncio.sleep(self.poll_interval)

    async def start(self):
        """Start polling."""
        if self._running:
            return

        logger.info(f"Starting sysfs poller (interval: {self.poll_interval * 1000}ms)...")

        # Initialize with current stats
        interfaces = self._get_interfaces()
        for ifname in interfaces:
            self._previous_stats[ifname] = self._read_stats(ifname)
            self._current_states[ifname] = TrafficState.UNKNOWN
            self._idle_counters[ifname] = 0

        logger.info(f"Monitoring interfaces: {interfaces}")

        self._running = True
        asyncio.create_task(self._poll_loop())

        logger.info("sysfs poller started")

    async def stop(self):
        """Stop polling."""
        logger.info("Stopping sysfs poller...")
        self._running = False
        logger.info("sysfs poller stopped")

    async def events(self) -> AsyncIterator[TrafficStateChange]:
        """Async iterator for traffic state change events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    def get_metrics(self, ifname: str) -> Optional[TrafficMetrics]:
        """Get current metrics for an interface."""
        return self._current_metrics.get(ifname)

    def get_all_metrics(self) -> dict[str, TrafficMetrics]:
        """Get current metrics for all interfaces."""
        return self._current_metrics.copy()

    def get_state(self, ifname: str) -> TrafficState:
        """Get current traffic state for an interface."""
        return self._current_states.get(ifname, TrafficState.UNKNOWN)

    @property
    def is_running(self) -> bool:
        return self._running


# Standalone usage example
async def main():
    """Example usage of SysfsPoller."""
    def on_change(event: TrafficStateChange):
        print(f"State change: {event.interface} -> {event.new_state.value}")
        print(f"  RX: {event.metrics.rx_bps:.2f} B/s, TX: {event.metrics.tx_bps:.2f} B/s")

    poller = SysfsPoller(
        poll_interval_ms=100,
        callback=on_change,
        idle_threshold_samples=10,
    )

    try:
        await poller.start()
        print("Polling traffic statistics. Press Ctrl+C to stop.")

        # Print metrics every second
        while True:
            await asyncio.sleep(1)
            metrics = poller.get_all_metrics()
            for ifname, m in metrics.items():
                print(f"{ifname}: state={m.state.value}, rx={m.rx_bps:.0f} B/s, tx={m.tx_bps:.0f} B/s")

    except KeyboardInterrupt:
        pass
    finally:
        await poller.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
