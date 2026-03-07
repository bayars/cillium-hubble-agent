"""
Hubble Integration Service - Consumes Cilium Hubble flows and updates topology.

Runs as a background task within the API process, connecting directly to
Hubble Relay to observe network flows and update link states in-memory.
"""

import asyncio
import logging
import os
from typing import Optional

from .hubble_monitor import HubbleMonitor, LinkStateChange as HubbleLinkStateChange
from .cilium_discovery import (
    CiliumEndpointDiscovery,
    EndpointEvent,
    EndpointEventType,
)
from .link_state_service import get_link_state_service
from .event_bus import get_event_bus
from ..models.schemas import Node, NodeStatus

logger = logging.getLogger(__name__)


class HubbleService:
    """
    Integrates Hubble flow monitoring and Cilium endpoint discovery
    directly into the API's link state service.
    """

    def __init__(
        self,
        hubble_relay_addr: str = "hubble-relay.kube-system.svc.cluster.local:4245",
        idle_timeout_seconds: float = 5.0,
        namespace_filter: Optional[str] = None,
    ):
        self.hubble_relay_addr = hubble_relay_addr
        self.idle_timeout_seconds = idle_timeout_seconds
        self.namespace_filter = namespace_filter

        self._hubble_monitor: Optional[HubbleMonitor] = None
        self._cilium_discovery: Optional[CiliumEndpointDiscovery] = None
        self._running = False
        self._flow_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start Hubble monitoring and Cilium discovery."""
        if self._running:
            return

        logger.info(f"Starting Hubble service (relay: {self.hubble_relay_addr})...")

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
            callback=self._on_flow_state_change,
        )
        await self._hubble_monitor.start()

        # Start flow observation in background
        self._running = True
        self._flow_task = asyncio.create_task(self._observe_flows())

        endpoints = self._cilium_discovery.get_all_endpoints()
        logger.info(f"Hubble service started. Discovered {len(endpoints)} endpoints.")

    async def stop(self):
        """Stop all monitoring."""
        if not self._running:
            return

        logger.info("Stopping Hubble service...")
        self._running = False

        if self._flow_task:
            self._flow_task.cancel()
            try:
                await self._flow_task
            except asyncio.CancelledError:
                pass

        if self._hubble_monitor:
            await self._hubble_monitor.stop()

        if self._cilium_discovery:
            await self._cilium_discovery.stop()

        logger.info("Hubble service stopped")

    def _on_flow_state_change(self, event: HubbleLinkStateChange):
        """Handle flow state change from Hubble monitor."""
        logger.info(
            f"Flow state change: {event.flow_key} "
            f"{event.old_state.value} -> {event.new_state.value}"
        )

        asyncio.create_task(
            get_event_bus().publish(
                "link_state_change",
                event.to_dict(),
                source="hubble",
            )
        )

    def _on_endpoint_event(self, event: EndpointEvent):
        """Handle endpoint discovery/removal from Cilium."""
        logger.info(f"Endpoint {event.type.value}: {event.endpoint.id}")
        asyncio.create_task(self._handle_endpoint_event(event))

    async def _handle_endpoint_event(self, event: EndpointEvent):
        """Process endpoint event and update topology."""
        service = get_link_state_service()
        endpoint = event.endpoint

        if event.type in (EndpointEventType.ADDED, EndpointEventType.MODIFIED):
            node = Node(
                id=endpoint.id,
                label=endpoint.pod_name or endpoint.name,
                type="pod",
                status=NodeStatus.UP
                if endpoint.state.value == "ready"
                else NodeStatus.UNKNOWN,
                ip_address=endpoint.ipv4_address,
                metadata={
                    "node_name": endpoint.node_name,
                    "namespace": endpoint.namespace,
                    "identity": endpoint.identity,
                },
            )
            await service.add_node(node)

        elif event.type == EndpointEventType.DELETED:
            await service.remove_node(endpoint.id)

    async def _observe_flows(self):
        """Background task to observe Hubble flows."""
        try:
            async for flow in self._hubble_monitor.observe_flows():
                change = self._hubble_monitor._update_flow_state(flow)
                if change:
                    await self._hubble_monitor._event_queue.put(change)
                    if self._hubble_monitor.callback:
                        self._hubble_monitor.callback(change)
                    logger.info(
                        f"Flow state change: {change.flow_key} "
                        f"{change.old_state.value} -> {change.new_state.value}"
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Flow observation error: {e}")

    @property
    def is_running(self) -> bool:
        return self._running


# Global instance
_hubble_service: Optional[HubbleService] = None


def get_hubble_service() -> Optional[HubbleService]:
    """Get the global Hubble service instance."""
    return _hubble_service


async def start_hubble_service():
    """Create and start the Hubble service from environment config."""
    global _hubble_service

    relay_addr = os.environ.get(
        "HUBBLE_RELAY_ADDR",
        "hubble-relay.kube-system.svc.cluster.local:4245",
    )
    idle_timeout = float(os.environ.get("IDLE_TIMEOUT_SECONDS", "5"))
    namespace = os.environ.get("NAMESPACE_FILTER")

    _hubble_service = HubbleService(
        hubble_relay_addr=relay_addr,
        idle_timeout_seconds=idle_timeout,
        namespace_filter=namespace,
    )
    await _hubble_service.start()


async def stop_hubble_service():
    """Stop the Hubble service."""
    global _hubble_service
    if _hubble_service:
        await _hubble_service.stop()
        _hubble_service = None
