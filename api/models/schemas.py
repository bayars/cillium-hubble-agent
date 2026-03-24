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

    ACTIVE = "active"  # Link up, traffic flowing
    IDLE = "idle"  # Link up, no traffic
    DOWN = "down"  # Link down
    UNKNOWN = "unknown"  # State not determined


class NodeStatus(str, Enum):
    """Node status enumeration."""

    UP = "up"
    DOWN = "down"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class LinkMetrics(BaseModel):
    """
    Traffic metrics for a link.

    NOTE on data sources:
    - Hubble mode: Only flow_count, flows_per_second, flows_forwarded,
      flows_dropped, and protocols are populated from real Hubble data.
      Hubble does NOT provide byte-rate or bandwidth data.
    - sysfs mode: rx_bps, tx_bps, rx_pps, tx_pps come from kernel counters.
    - External push: Any field can be set via PUT /api/links/{id}/metrics
      by external tools (e.g., gNMI collectors, SNMP pollers).
    """

    # Byte-rate metrics (from sysfs/kernel counters or external push, NOT from Hubble)
    rx_bps: float = Field(0.0, description="Receive bytes per second (sysfs/external only)")
    tx_bps: float = Field(0.0, description="Transmit bytes per second (sysfs/external only)")
    rx_pps: float = Field(0.0, description="Receive packets per second (sysfs/external only)")
    tx_pps: float = Field(0.0, description="Transmit packets per second (sysfs/external only)")
    rx_bytes_total: int = Field(0, description="Total bytes received")
    tx_bytes_total: int = Field(0, description="Total bytes transmitted")
    utilization: float = Field(0.0, ge=0, le=1, description="Link utilization 0-1 (sysfs/external only)")
    latency_ms: Optional[float] = Field(None, description="Link latency in ms")
    packet_loss: Optional[float] = Field(None, description="Packet loss percentage")

    # Flow-based metrics (from Hubble)
    flow_count: int = Field(0, description="Total flow events observed (Hubble)")
    flows_per_second: float = Field(0.0, description="Flow event rate (Hubble)")
    flows_forwarded: int = Field(0, description="Flows with FORWARDED verdict (Hubble)")
    flows_dropped: int = Field(0, description="Flows with DROPPED verdict (Hubble)")
    active_connections: int = Field(0, description="Currently active connections (Hubble)")
    protocols: dict = Field(default_factory=dict, description="Protocol breakdown {TCP: N, UDP: M} (Hubble)")
    data_source: str = Field("none", description="Source of metrics: hubble, sysfs, iperf3, external")

    class Config:
        json_schema_extra = {
            "example": {
                "rx_bps": 0.0,
                "tx_bps": 0.0,
                "flow_count": 1523,
                "flows_per_second": 42.5,
                "flows_forwarded": 1500,
                "flows_dropped": 23,
                "active_connections": 8,
                "protocols": {"TCP": 1200, "UDP": 323},
                "data_source": "hubble",
            }
        }


class Node(BaseModel):
    """Represents a network node (router/device)."""

    id: str = Field(..., description="Unique node identifier")
    lab: str = Field("default", description="Lab name this node belongs to")
    label: str = Field(..., description="Display label for the node")
    type: str = Field("router", description="Node type (router, switch, host)")
    status: NodeStatus = Field(NodeStatus.UNKNOWN, description="Node status")
    ip_address: Optional[str] = Field(None, description="Management IP address")
    platform: Optional[str] = Field(
        None, description="Platform/OS (e.g., 'srlinux', 'ceos')"
    )
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
                "metadata": {"version": "23.10.1"},
            }
        }


class Link(BaseModel):
    """Represents a network link between two nodes."""

    id: str = Field(..., description="Unique link identifier")
    lab: str = Field("default", description="Lab name this link belongs to")
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    source_interface: str = Field(..., description="Interface name on source node")
    target_interface: str = Field(..., description="Interface name on target node")
    state: LinkState = Field(LinkState.UNKNOWN, description="Current link state")
    metrics: LinkMetrics = Field(
        default_factory=LinkMetrics, description="Traffic metrics"
    )
    speed_mbps: int = Field(0, description="Link speed in Mbps")
    mtu: int = Field(1500, description="MTU size")
    last_updated: datetime = Field(
        default_factory=datetime.now, description="Last update time"
    )
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
                    "utilization": 0.45,
                },
                "speed_mbps": 10000,
                "mtu": 9000,
            }
        }


class TopologyResponse(BaseModel):
    """Complete network topology for Cytoscape."""

    nodes: list[Node] = Field(default_factory=list, description="List of network nodes")
    edges: list[Link] = Field(default_factory=list, description="List of network links")
    timestamp: datetime = Field(
        default_factory=datetime.now, description="Response timestamp"
    )
    version: str = Field("1.0", description="API version")

    class Config:
        json_schema_extra = {
            "example": {
                "nodes": [
                    {"id": "router1", "label": "R1", "type": "router", "status": "up"},
                    {"id": "router2", "label": "R2", "type": "router", "status": "up"},
                ],
                "edges": [
                    {
                        "id": "link1",
                        "source": "router1",
                        "target": "router2",
                        "source_interface": "eth1",
                        "target_interface": "eth1",
                        "state": "active",
                    }
                ],
                "timestamp": "2024-01-15T10:30:00Z",
                "version": "1.0",
            }
        }


class LinkStateEvent(BaseModel):
    """Event representing a link state change."""

    link_id: str = Field(..., description="Link identifier")
    interface: str = Field(..., description="Interface name")
    old_state: LinkState = Field(..., description="Previous state")
    new_state: LinkState = Field(..., description="New state")
    timestamp: datetime = Field(
        default_factory=datetime.now, description="Event timestamp"
    )
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
                "source": "agent",
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
                "source": "netlink",
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
    connected_clients: int = Field(0)
    monitored_links: int = Field(0)
    hubble_connected: bool = Field(False)
    timestamp: datetime = Field(default_factory=datetime.now)


class ErrorResponse(BaseModel):
    """Error response."""

    error: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)


# ============================================================================
# Lab Management Models
# ============================================================================


# ============================================================================
# Interface Metrics Models (sidecar agent)
# ============================================================================


class InterfaceState(str, Enum):
    """Interface operational state."""

    UP = "up"
    DOWN = "down"
    UNKNOWN = "unknown"


class InterfaceMetrics(BaseModel):
    """Per-interface traffic metrics collected by sidecar agent."""

    name: str = Field(..., description="Interface name (e.g., ethernet-1/1, eth0, mgmt0)")
    state: InterfaceState = Field(InterfaceState.UNKNOWN, description="Operational state")
    rx_bps: float = Field(0.0, description="Receive bytes per second")
    tx_bps: float = Field(0.0, description="Transmit bytes per second")
    rx_pps: float = Field(0.0, description="Receive packets per second")
    tx_pps: float = Field(0.0, description="Transmit packets per second")
    rx_bytes_total: int = Field(0, description="Total bytes received")
    tx_bytes_total: int = Field(0, description="Total bytes transmitted")
    rx_packets_total: int = Field(0, description="Total packets received")
    tx_packets_total: int = Field(0, description="Total packets transmitted")
    rx_errors: int = Field(0, description="Receive errors")
    tx_errors: int = Field(0, description="Transmit errors")
    rx_dropped: int = Field(0, description="Receive dropped")
    tx_dropped: int = Field(0, description="Transmit dropped")
    last_updated: datetime = Field(default_factory=datetime.now)


class InterfaceMetricsPush(BaseModel):
    """Bulk push of interface metrics from sidecar agent."""

    node_id: str = Field(..., description="Node identifier (must match an existing node)")
    interfaces: list[InterfaceMetrics] = Field(..., description="Metrics for each interface")
    poll_interval_ms: int = Field(1000, description="Polling interval used by sidecar")
    data_source: str = Field("sysfs", description="Source: sysfs")


class NodeInterfacesResponse(BaseModel):
    """Response containing all interfaces for a node."""

    node_id: str
    node_label: str = ""
    interfaces: list[InterfaceMetrics] = Field(default_factory=list)
    count: int = 0
    timestamp: datetime = Field(default_factory=datetime.now)


class LabStatus(str, Enum):
    """Lab deployment status."""

    PENDING = "pending"  # CRD created, waiting for controller
    DEPLOYING = "deploying"  # Pods being created
    RUNNING = "running"  # All pods ready
    FAILED = "failed"  # Deployment error
    DELETED = "deleted"  # Lab deleted


class Lab(BaseModel):
    """Represents a deployed lab."""

    name: str = Field(..., description="Lab identifier (used as ID prefix)")
    namespace: str = Field("clab", description="Kubernetes namespace")
    status: LabStatus = Field(LabStatus.PENDING, description="Deployment status")
    topology_name: str = Field(..., description="Clabernetes Topology CRD name")
    nodes_count: int = Field(0, description="Number of nodes in the lab")
    links_count: int = Field(0, description="Number of links in the lab")
    created_at: datetime = Field(default_factory=datetime.now)
    message: Optional[str] = Field(None, description="Status message or error")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "datacenter1",
                "namespace": "clab",
                "status": "running",
                "topology_name": "datacenter1",
                "nodes_count": 5,
                "links_count": 6,
                "created_at": "2024-01-15T10:30:00Z",
            }
        }


class LabDeployRequest(BaseModel):
    """Request to deploy a new lab."""

    name: str = Field(..., description="Lab name (becomes ID prefix for nodes/links)")
    namespace: str = Field("clab", description="Target Kubernetes namespace")
    containerlab_yaml: Optional[str] = Field(
        None, description="Containerlab topology YAML content"
    )
    clabernetes_yaml: Optional[str] = Field(
        None, description="Full Clabernetes Topology CRD YAML"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "datacenter1",
                "namespace": "clab",
                "containerlab_yaml": 'name: datacenter1\ntopology:\n  nodes:\n    spine1:\n      kind: srl\n  links:\n    - endpoints: ["spine1:e1-1", "leaf1:eth1"]',
            }
        }


class LabDeployResponse(BaseModel):
    """Response from lab deployment."""

    lab: str = Field(..., description="Lab name")
    status: LabStatus = Field(..., description="Current status")
    nodes_discovered: int = Field(0, description="Number of nodes parsed")
    links_discovered: int = Field(0, description="Number of links parsed")
    topology_crd: str = Field(..., description="Name of created Clabernetes CRD")
    node_ids: list[str] = Field(default_factory=list, description="Created node IDs")
    link_ids: list[str] = Field(default_factory=list, description="Created link IDs")
    message: Optional[str] = Field(None, description="Status message")

    class Config:
        json_schema_extra = {
            "example": {
                "lab": "datacenter1",
                "status": "deploying",
                "nodes_discovered": 5,
                "links_discovered": 6,
                "topology_crd": "datacenter1",
                "node_ids": ["datacenter1/spine1", "datacenter1/leaf1"],
                "link_ids": ["datacenter1/spine1-leaf1"],
            }
        }


class LabListResponse(BaseModel):
    """Response containing list of labs."""

    labs: list[Lab] = Field(default_factory=list)
    count: int = Field(0)
    timestamp: datetime = Field(default_factory=datetime.now)
