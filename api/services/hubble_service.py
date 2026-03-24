"""
Hubble Integration Service - Consumes Cilium Hubble flows and updates topology.

Runs as a background task within the API process, connecting directly to
Hubble Relay to observe network flows and update link states in-memory.

NOTE: Hubble provides flow-level data (connection presence, verdict, protocol).
It does NOT provide bandwidth/byte-rate metrics. The metrics exposed here
are flow counts and rates, which is what Hubble actually observes.
"""

import asyncio
import logging
import os
from typing import Optional

from .hubble_monitor import HubbleMonitor, LinkStateChange as HubbleLinkStateChange, FlowMetrics
from .cilium_discovery import (
    CiliumEndpointDiscovery,
    EndpointEvent,
    EndpointEventType,
)
from .link_state_service import get_link_state_service
from .event_bus import get_event_bus
from ..models.schemas import Node, NodeStatus, LinkMetrics

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
        use_tls: bool = False,
    ):
        self.hubble_relay_addr = hubble_relay_addr
        self.idle_timeout_seconds = idle_timeout_seconds
        self.namespace_filter = namespace_filter
        self.use_tls = use_tls

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
            use_tls=self.use_tls,
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

    def _flow_metrics_to_link_metrics(self, flow_metrics: Optional[FlowMetrics]) -> Optional[LinkMetrics]:
        """Convert Hubble FlowMetrics to API LinkMetrics."""
        if not flow_metrics:
            return None

        return LinkMetrics(
            flow_count=flow_metrics.flows_total,
            flows_per_second=flow_metrics.flows_per_second,
            flows_forwarded=flow_metrics.flows_forwarded,
            flows_dropped=flow_metrics.flows_dropped,
            active_connections=flow_metrics.active_connections,
            protocols=flow_metrics.protocols,
            # Explicitly zero - Hubble does not provide these
            rx_bps=0.0,
            tx_bps=0.0,
            rx_pps=0.0,
            tx_pps=0.0,
            utilization=0.0,
            data_source="hubble",
        )

    def _on_flow_state_change(self, event: HubbleLinkStateChange):
        """Handle flow state change from Hubble monitor."""
        logger.info(
            f"Flow state change: {event.flow_key} "
            f"{event.old_state.value} -> {event.new_state.value}"
        )

        event_data = event.to_dict()
        if event.metrics:
            event_data["flow_metrics"] = event.metrics.to_dict()

        asyncio.create_task(
            get_event_bus().publish(
                "link_state_change",
                event_data,
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
        """Background task to observe Hubble flows and update link metrics."""
        try:
            async for flow in self._hubble_monitor.observe_flows():
                change = self._hubble_monitor._update_flow_state(flow)
                if change:
                    await self._hubble_monitor._event_queue.put(change)
                    if self._hubble_monitor.callback:
                        self._hubble_monitor.callback(change)

                    # Update link metrics with real Hubble flow data
                    link_metrics = self._flow_metrics_to_link_metrics(change.metrics)
                    if link_metrics:
                        await self._update_link_for_flow(
                            change.flow_key, change.new_state.value, link_metrics
                        )

                    logger.info(
                        f"Flow state change: {change.flow_key} "
                        f"{change.old_state.value} -> {change.new_state.value}"
                    )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Flow observation error: {e}")

    async def _update_link_for_flow(self, flow_key: str, state: str, metrics: LinkMetrics):
        """Try to match a Hubble flow to a topology link and update its metrics."""
        service = get_link_state_service()
        all_links = await service.get_all_links()

        # Try to find a link that matches this flow's source/destination
        for link in all_links:
            if flow_key in (
                f"{link.source}->{link.target}",
                f"{link.target}->{link.source}",
            ):
                await service.update_link_metrics(link.id, metrics)
                break

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
    use_tls = os.environ.get("HUBBLE_TLS", "false").lower() == "true"

    _hubble_service = HubbleService(
        hubble_relay_addr=relay_addr,
        idle_timeout_seconds=idle_timeout,
        namespace_filter=namespace,
        use_tls=use_tls,
    )
    await _hubble_service.start()


async def stop_hubble_service():
    """Stop the Hubble service."""
    global _hubble_service
    if _hubble_service:
        await _hubble_service.stop()
        _hubble_service = None
