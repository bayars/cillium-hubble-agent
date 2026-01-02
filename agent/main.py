"""
Network Monitor Agent - Main entry point.

Monitors network interfaces for:
- Link state changes (up/down) via Netlink or Hubble
- Traffic state (active/idle) via sysfs polling or Hubble flows
- Publishes events to API server

Discovery Modes:
- sysfs: Uses Netlink + sysfs polling (default for standalone/VM)
- hubble: Uses Cilium Hubble Relay for K8s deployments

Usage:
    # sysfs mode (default)
    python -m agent.main --api-url http://localhost:8000/api/events

    # hubble mode (Kubernetes)
    python -m agent.main --discovery-mode hubble --hubble-relay hubble-relay:4245
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from enum import Enum
from typing import Optional, Union

from .interface_manager import InterfaceManager, StateChangeEvent
from .event_publisher import (
    EventPublisher,
    HttpPublisher,
    WebSocketPublisher,
    InMemoryPublisher,
    create_publisher,
)

logger = logging.getLogger(__name__)


class DiscoveryMode(str, Enum):
    """Discovery mode for interface/endpoint detection."""
    SYSFS = "sysfs"    # Netlink + sysfs (standalone)
    HUBBLE = "hubble"  # Cilium Hubble (Kubernetes)


class NetworkMonitorAgent:
    """
    Main agent class that coordinates interface monitoring and event publishing.

    Supports two discovery modes:
    - sysfs: Uses Netlink + sysfs for standalone/VM deployments
    - hubble: Uses Cilium Hubble for Kubernetes deployments
    """

    def __init__(
        self,
        api_url: Optional[str] = None,
        ws_url: Optional[str] = None,
        poll_interval_ms: int = 100,
        interface_filter: Optional[list[str]] = None,
        discovery_mode: DiscoveryMode = DiscoveryMode.SYSFS,
        hubble_relay_addr: str = "hubble-relay:4245",
        idle_timeout_seconds: float = 5.0,
        namespace_filter: Optional[str] = None,
    ):
        """
        Initialize the agent.

        Args:
            api_url: HTTP API endpoint for events (e.g., http://localhost:8000/api/events)
            ws_url: WebSocket endpoint for events (e.g., ws://localhost:8000/ws/agent)
            poll_interval_ms: Traffic polling interval (sysfs mode)
            interface_filter: List of interfaces to monitor (sysfs mode)
            discovery_mode: Discovery backend (sysfs or hubble)
            hubble_relay_addr: Hubble Relay address (hubble mode)
            idle_timeout_seconds: Seconds before marking flow as idle (hubble mode)
            namespace_filter: Kubernetes namespace filter (hubble mode)
        """
        self.api_url = api_url
        self.ws_url = ws_url
        self.poll_interval_ms = poll_interval_ms
        self.interface_filter = interface_filter
        self.discovery_mode = discovery_mode
        self.hubble_relay_addr = hubble_relay_addr
        self.idle_timeout_seconds = idle_timeout_seconds
        self.namespace_filter = namespace_filter

        self._running = False
        self._interface_manager: Optional[InterfaceManager] = None
        self._hubble_monitor = None
        self._cilium_discovery = None
        self._publisher: Optional[EventPublisher] = None

    def _on_state_change(self, event: StateChangeEvent):
        """Handle interface state change events."""
        logger.info(
            f"State change: {event.interface} "
            f"{event.old_state.value} -> {event.new_state.value}"
        )

        if self._publisher:
            # Queue event for publishing
            asyncio.create_task(self._publish_event(event))

    async def _publish_event(self, event: StateChangeEvent):
        """Publish state change event."""
        try:
            result = await self._publisher.publish(event.to_dict())
            if not result.success:
                logger.warning(f"Failed to publish event: {result.message}")
        except Exception as e:
            logger.error(f"Error publishing event: {e}")

    def _on_hubble_state_change(self, event):
        """Handle state change from Hubble monitor."""
        logger.info(
            f"Flow state change: {event.flow_key} "
            f"{event.old_state.value} -> {event.new_state.value}"
        )

        if self._publisher:
            asyncio.create_task(self._publisher.publish(event.to_dict()))

    def _on_endpoint_event(self, event):
        """Handle endpoint events from Cilium discovery."""
        logger.info(f"Endpoint {event.type.value}: {event.endpoint.id}")

        if self._publisher:
            asyncio.create_task(self._publisher.publish({
                "type": f"endpoint_{event.type.value.lower()}",
                "endpoint": event.endpoint.to_dict(),
                "timestamp": event.timestamp.isoformat(),
            }))

    async def start(self):
        """Start the agent."""
        if self._running:
            return

        logger.info(f"Starting Network Monitor Agent (mode: {self.discovery_mode.value})...")

        # Create publisher
        if self.ws_url:
            publisher = WebSocketPublisher(self.ws_url)
        elif self.api_url:
            publisher = HttpPublisher(self.api_url)
        else:
            logger.info("No API URL configured, using in-memory publisher")
            publisher = InMemoryPublisher()

        self._publisher = EventPublisher(publisher)
        await self._publisher.start()

        # Start appropriate discovery backend
        if self.discovery_mode == DiscoveryMode.HUBBLE:
            await self._start_hubble_mode()
        else:
            await self._start_sysfs_mode()

        self._running = True
        logger.info("Network Monitor Agent started")

    async def _start_sysfs_mode(self):
        """Start sysfs/Netlink based monitoring."""
        logger.info("Starting sysfs/Netlink monitoring...")

        self._interface_manager = InterfaceManager(
            poll_interval_ms=self.poll_interval_ms,
            interface_filter=self.interface_filter,
            state_change_callback=self._on_state_change,
        )
        await self._interface_manager.start()

        # Log initial state
        states = self._interface_manager.get_interface_states()
        logger.info(f"Monitoring {len(states)} interfaces: {list(states.keys())}")
        for ifname, state in states.items():
            logger.info(f"  {ifname}: {state.value}")

    async def _start_hubble_mode(self):
        """Start Hubble-based monitoring for Kubernetes."""
        logger.info(f"Starting Hubble monitoring (relay: {self.hubble_relay_addr})...")

        # Import Hubble components
        from .hubble_monitor import HubbleMonitor
        from .cilium_discovery import CiliumEndpointDiscovery

        # Start Cilium endpoint discovery
        self._cilium_discovery = CiliumEndpointDiscovery(
            namespace=self.namespace_filter,
            callback=self._on_endpoint_event,
        )
        await self._cilium_discovery.start()

        # Start Hubble flow monitor
        self._hubble_monitor = HubbleMonitor(
            relay_addr=self.hubble_relay_addr,
            idle_timeout_seconds=self.idle_timeout_seconds,
            callback=self._on_hubble_state_change,
        )
        await self._hubble_monitor.start()

        # Log initial state
        endpoints = self._cilium_discovery.get_all_endpoints()
        logger.info(f"Discovered {len(endpoints)} Cilium endpoints")
        for ep_id, ep in endpoints.items():
            logger.info(f"  {ep_id}: {ep.ipv4_address} (node: {ep.node_name})")

    async def stop(self):
        """Stop the agent."""
        if not self._running:
            return

        logger.info("Stopping Network Monitor Agent...")
        self._running = False

        # Stop discovery components
        if self._interface_manager:
            await self._interface_manager.stop()

        if self._hubble_monitor:
            await self._hubble_monitor.stop()

        if self._cilium_discovery:
            await self._cilium_discovery.stop()

        if self._publisher:
            await self._publisher.stop()

        logger.info("Network Monitor Agent stopped")

    async def run_forever(self):
        """Run the agent until interrupted."""
        await self.start()

        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    def get_status(self) -> dict:
        """Get agent status."""
        status = {
            "running": self._running,
            "publisher_connected": self._publisher.is_connected if self._publisher else False,
            "interfaces": {},
            "timestamp": datetime.now().isoformat(),
        }

        if self._interface_manager:
            for ifname, info in self._interface_manager.get_all_interfaces().items():
                status["interfaces"][ifname] = info.to_dict()

        return status


def setup_logging(level: str = "INFO"):
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Network Monitor Agent - monitors interface state changes"
    )

    # Discovery mode
    parser.add_argument(
        "--discovery-mode",
        type=str,
        default=os.environ.get("DISCOVERY_MODE", "sysfs"),
        choices=["sysfs", "hubble"],
        help="Discovery mode: sysfs (Netlink/sysfs) or hubble (Cilium Hubble)",
    )

    # API endpoints
    parser.add_argument(
        "--api-url",
        type=str,
        default=os.environ.get("API_URL"),
        help="HTTP API endpoint URL for publishing events",
    )

    parser.add_argument(
        "--ws-url",
        type=str,
        default=os.environ.get("WS_URL"),
        help="WebSocket endpoint URL for publishing events",
    )

    # sysfs mode options
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_MS", "100")),
        help="Traffic polling interval in milliseconds (sysfs mode, default: 100)",
    )

    parser.add_argument(
        "--interfaces",
        type=str,
        default=os.environ.get("INTERFACES"),
        help="Comma-separated list of interfaces to monitor (sysfs mode)",
    )

    # Hubble mode options
    parser.add_argument(
        "--hubble-relay",
        type=str,
        default=os.environ.get("HUBBLE_RELAY_ADDR", "hubble-relay:4245"),
        help="Hubble Relay address (hubble mode, default: hubble-relay:4245)",
    )

    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("IDLE_TIMEOUT_SECONDS", "5.0")),
        help="Seconds without traffic before marking idle (hubble mode, default: 5.0)",
    )

    parser.add_argument(
        "--namespace-filter",
        type=str,
        default=os.environ.get("NAMESPACE_FILTER"),
        help="Kubernetes namespace to filter (hubble mode, None = all)",
    )

    # General options
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )

    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()
    setup_logging(args.log_level)

    # Parse discovery mode
    discovery_mode = DiscoveryMode(args.discovery_mode)

    # Parse interface filter (sysfs mode)
    interface_filter = None
    if args.interfaces:
        interface_filter = [iface.strip() for iface in args.interfaces.split(",")]

    # Create agent
    agent = NetworkMonitorAgent(
        api_url=args.api_url,
        ws_url=args.ws_url,
        poll_interval_ms=args.poll_interval,
        interface_filter=interface_filter,
        discovery_mode=discovery_mode,
        hubble_relay_addr=args.hubble_relay,
        idle_timeout_seconds=args.idle_timeout,
        namespace_filter=args.namespace_filter,
    )

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def shutdown():
        logger.info("Shutdown signal received")
        asyncio.create_task(agent.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    # Run agent
    try:
        await agent.run_forever()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
