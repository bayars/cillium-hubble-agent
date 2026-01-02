"""
Pydantic models for API schemas.

Defines the data structures for:
- Nodes (routers/devices)
- Links (connections between nodes)
- Topology (complete network graph)
- Events (state changes)
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class LinkState(str, Enum):
    """Link state enumeration."""
    ACTIVE = "active"   # Link up, traffic flowing
    IDLE = "idle"       # Link up, no traffic
    DOWN = "down"       # Link down
    UNKNOWN = "unknown" # State not determined


class NodeStatus(str, Enum):
    """Node status enumeration."""
    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class LinkMetrics(BaseModel):
    """Traffic metrics for a link."""
    rx_bps: float = Field(0.0, description="Receive bytes per second")
    tx_bps: float = Field(0.0, description="Transmit bytes per second")
    rx_pps: float = Field(0.0, description="Receive packets per second")
    tx_pps: float = Field(0.0, description="Transmit packets per second")
    rx_bytes_total: int = Field(0, description="Total bytes received")
    tx_bytes_total: int = Field(0, description="Total bytes transmitted")
    utilization: float = Field(0.0, ge=0, le=1, description="Link utilization (0-1)")
    latency_ms: Optional[float] = Field(None, description="Link latency in ms")
    packet_loss: Optional[float] = Field(None, description="Packet loss percentage")

    class Config:
        json_schema_extra = {
            "example": {
                "rx_bps": 1250000.0,
                "tx_bps": 980000.0,
                "rx_pps": 1000.0,
                "tx_pps": 800.0,
                "rx_bytes_total": 1234567890,
                "tx_bytes_total": 987654321,
                "utilization": 0.45,
                "latency_ms": 2.5,
                "packet_loss": 0.01
            }
        }


class Node(BaseModel):
    """Represents a network node (router/device)."""
    id: str = Field(..., description="Unique node identifier")
    label: str = Field(..., description="Display label for the node")
    type: str = Field("router", description="Node type (router, switch, host)")
    status: NodeStatus = Field(NodeStatus.UNKNOWN, description="Node status")
    ip_address: Optional[str] = Field(None, description="Management IP address")
    platform: Optional[str] = Field(None, description="Platform/OS (e.g., 'srlinux', 'ceos')")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "router1",
                "label": "R1",
                "type": "router",
                "status": "up",
                "ip_address": "10.0.0.1",
                "platform": "srlinux",
                "metadata": {"version": "23.10.1"}
            }
        }


class Link(BaseModel):
    """Represents a network link between two nodes."""
    id: str = Field(..., description="Unique link identifier")
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    source_interface: str = Field(..., description="Interface name on source node")
    target_interface: str = Field(..., description="Interface name on target node")
    state: LinkState = Field(LinkState.UNKNOWN, description="Current link state")
    metrics: LinkMetrics = Field(default_factory=LinkMetrics, description="Traffic metrics")
    speed_mbps: int = Field(0, description="Link speed in Mbps")
    mtu: int = Field(1500, description="MTU size")
    last_updated: datetime = Field(default_factory=datetime.now, description="Last update time")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")

    class Config:
        json_schema_extra = {
            "example": {
                "id": "link1",
                "source": "router1",
                "target": "router2",
                "source_interface": "ethernet-1/1",
                "target_interface": "ethernet-1/1",
                "state": "active",
                "metrics": {
                    "rx_bps": 1250000.0,
                    "tx_bps": 980000.0,
                    "utilization": 0.45
                },
                "speed_mbps": 10000,
                "mtu": 9000
            }
        }


class TopologyResponse(BaseModel):
    """Complete network topology for Cytoscape."""
    nodes: list[Node] = Field(default_factory=list, description="List of network nodes")
    edges: list[Link] = Field(default_factory=list, description="List of network links")
    timestamp: datetime = Field(default_factory=datetime.now, description="Response timestamp")
    version: str = Field("1.0", description="API version")

    class Config:
        json_schema_extra = {
            "example": {
                "nodes": [
                    {"id": "router1", "label": "R1", "type": "router", "status": "up"},
                    {"id": "router2", "label": "R2", "type": "router", "status": "up"}
                ],
                "edges": [
                    {
                        "id": "link1",
                        "source": "router1",
                        "target": "router2",
                        "source_interface": "eth1",
                        "target_interface": "eth1",
                        "state": "active"
                    }
                ],
                "timestamp": "2024-01-15T10:30:00Z",
                "version": "1.0"
            }
        }


class LinkStateEvent(BaseModel):
    """Event representing a link state change."""
    link_id: str = Field(..., description="Link identifier")
    interface: str = Field(..., description="Interface name")
    old_state: LinkState = Field(..., description="Previous state")
    new_state: LinkState = Field(..., description="New state")
    timestamp: datetime = Field(default_factory=datetime.now, description="Event timestamp")
    source: str = Field("agent", description="Event source (agent, snmp, gnmi)")
    metrics: Optional[LinkMetrics] = Field(None, description="Current metrics")

    class Config:
        json_schema_extra = {
            "example": {
                "link_id": "link1",
                "interface": "eth1",
                "old_state": "active",
                "new_state": "down",
                "timestamp": "2024-01-15T10:30:00Z",
                "source": "agent"
            }
        }


class InterfaceEvent(BaseModel):
    """Event from agent about interface state change."""
    interface: str = Field(..., description="Interface name")
    ifindex: int = Field(0, description="Interface index")
    old_state: str = Field(..., description="Previous state")
    new_state: str = Field(..., description="New state")
    operstate: str = Field("unknown", description="Operational state")
    timestamp: datetime = Field(default_factory=datetime.now)
    source: str = Field("agent", description="Event source")

    class Config:
        json_schema_extra = {
            "example": {
                "interface": "eth1",
                "ifindex": 2,
                "old_state": "active",
                "new_state": "down",
                "operstate": "down",
                "timestamp": "2024-01-15T10:30:00Z",
                "source": "netlink"
            }
        }


class LinksResponse(BaseModel):
    """Response containing all links."""
    links: list[Link] = Field(default_factory=list)
    count: int = Field(0)
    timestamp: datetime = Field(default_factory=datetime.now)


class HealthResponse(BaseModel):
    """Health check response."""
    status: str = Field("healthy")
    version: str = Field("1.0.0")
    uptime_seconds: float = Field(0.0)
    connected_agents: int = Field(0)
    monitored_links: int = Field(0)
    timestamp: datetime = Field(default_factory=datetime.now)


class ErrorResponse(BaseModel):
    """Error response."""
    error: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)
