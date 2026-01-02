"""
Link State Service - Manages network topology and link states.

Provides:
- Topology storage and retrieval
- Link state updates from agents
- State change event generation
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from ..models.schemas import (
    Node, Link, LinkState, LinkMetrics, NodeStatus,
    TopologyResponse, LinkStateEvent, InterfaceEvent,
)
from .event_bus import get_event_bus

logger = logging.getLogger(__name__)


class LinkStateService:
    """
    Service for managing link states and topology.

    Maintains in-memory state of the network topology and
    handles updates from monitoring agents.
    """

    def __init__(self):
        self._nodes: dict[str, Node] = {}
        self._links: dict[str, Link] = {}
        self._interface_to_link: dict[str, str] = {}  # interface -> link_id
        self._lock = asyncio.Lock()
        self._started_at = datetime.now()

    async def initialize_topology(self, nodes: list[Node], links: list[Link]):
        """
        Initialize topology with nodes and links.

        Args:
            nodes: List of network nodes
            links: List of network links
        """
        async with self._lock:
            self._nodes = {n.id: n for n in nodes}
            self._links = {l.id: l for l in links}

            # Build interface -> link mapping
            self._interface_to_link.clear()
            for link in links:
                self._interface_to_link[link.source_interface] = link.id
                self._interface_to_link[link.target_interface] = link.id

        logger.info(
            f"Initialized topology: {len(nodes)} nodes, {len(links)} links"
        )

    async def get_topology(self) -> TopologyResponse:
        """Get complete network topology."""
        async with self._lock:
            return TopologyResponse(
                nodes=list(self._nodes.values()),
                edges=list(self._links.values()),
                timestamp=datetime.now(),
            )

    async def get_all_links(self) -> list[Link]:
        """Get all links."""
        async with self._lock:
            return list(self._links.values())

    async def get_link(self, link_id: str) -> Optional[Link]:
        """Get a specific link by ID."""
        async with self._lock:
            return self._links.get(link_id)

    async def get_link_by_interface(self, interface: str) -> Optional[Link]:
        """Get link by interface name."""
        async with self._lock:
            link_id = self._interface_to_link.get(interface)
            if link_id:
                return self._links.get(link_id)
            return None

    async def update_link_state(
        self,
        link_id: str,
        new_state: LinkState,
        metrics: Optional[LinkMetrics] = None,
        source: str = "agent"
    ) -> Optional[LinkStateEvent]:
        """
        Update link state and emit event if changed.

        Args:
            link_id: Link identifier
            new_state: New link state
            metrics: Optional updated metrics
            source: Update source

        Returns:
            Event if state changed, None otherwise
        """
        async with self._lock:
            link = self._links.get(link_id)
            if not link:
                logger.warning(f"Unknown link: {link_id}")
                return None

            old_state = link.state

            # Update state
            link.state = new_state
            link.last_updated = datetime.now()

            if metrics:
                link.metrics = metrics

        # Emit event if state changed
        if old_state != new_state:
            event = LinkStateEvent(
                link_id=link_id,
                interface=link.source_interface,
                old_state=old_state,
                new_state=new_state,
                timestamp=datetime.now(),
                source=source,
                metrics=link.metrics,
            )

            # Publish to event bus
            await get_event_bus().publish(
                "link_state_change",
                event.model_dump(mode='json'),
                source=source,
            )

            logger.info(
                f"Link {link_id} state changed: "
                f"{old_state.value} -> {new_state.value}"
            )
            return event

        return None

    async def handle_agent_event(self, event: InterfaceEvent) -> Optional[LinkStateEvent]:
        """
        Handle state change event from monitoring agent.

        Args:
            event: Interface event from agent

        Returns:
            Link state event if applicable
        """
        # Find link by interface
        link = await self.get_link_by_interface(event.interface)

        if not link:
            # Interface not in topology, might need to add
            logger.debug(f"No link found for interface {event.interface}")
            return None

        # Map agent state to link state
        state_map = {
            "active": LinkState.ACTIVE,
            "idle": LinkState.IDLE,
            "down": LinkState.DOWN,
            "up_active": LinkState.ACTIVE,
            "up_idle": LinkState.IDLE,
        }

        new_state = state_map.get(event.new_state.lower(), LinkState.UNKNOWN)

        return await self.update_link_state(
            link.id,
            new_state,
            source=event.source,
        )

    async def update_link_metrics(
        self,
        link_id: str,
        metrics: LinkMetrics
    ):
        """Update link metrics without changing state."""
        async with self._lock:
            link = self._links.get(link_id)
            if link:
                link.metrics = metrics
                link.last_updated = datetime.now()

    async def add_node(self, node: Node):
        """Add a node to the topology."""
        async with self._lock:
            self._nodes[node.id] = node

        await get_event_bus().publish(
            "node_added",
            node.model_dump(mode='json'),
        )

    async def add_link(self, link: Link):
        """Add a link to the topology."""
        async with self._lock:
            self._links[link.id] = link
            self._interface_to_link[link.source_interface] = link.id
            self._interface_to_link[link.target_interface] = link.id

        await get_event_bus().publish(
            "link_added",
            link.model_dump(mode='json'),
        )

    async def remove_node(self, node_id: str):
        """Remove a node from the topology."""
        async with self._lock:
            if node_id in self._nodes:
                del self._nodes[node_id]

        await get_event_bus().publish(
            "node_removed",
            {"node_id": node_id},
        )

    async def remove_link(self, link_id: str):
        """Remove a link from the topology."""
        async with self._lock:
            link = self._links.pop(link_id, None)
            if link:
                self._interface_to_link.pop(link.source_interface, None)
                self._interface_to_link.pop(link.target_interface, None)

        await get_event_bus().publish(
            "link_removed",
            {"link_id": link_id},
        )

    def get_stats(self) -> dict:
        """Get service statistics."""
        states = {}
        for link in self._links.values():
            state = link.state.value
            states[state] = states.get(state, 0) + 1

        return {
            "node_count": len(self._nodes),
            "link_count": len(self._links),
            "link_states": states,
            "uptime_seconds": (datetime.now() - self._started_at).total_seconds(),
        }


# Global service instance
_link_state_service: Optional[LinkStateService] = None


def get_link_state_service() -> LinkStateService:
    """Get the global link state service instance."""
    global _link_state_service
    if _link_state_service is None:
        _link_state_service = LinkStateService()
    return _link_state_service


def reset_link_state_service():
    """Reset the global service (for testing)."""
    global _link_state_service
    _link_state_service = None
