"""
Hubble Monitor - Network flow monitoring via Cilium Hubble Relay gRPC API.

Provides real-time visibility into L3/L4 network flows by connecting to
Hubble Relay and streaming flow events. Detects:
- Active traffic: flows observed for endpoint pair
- Idle: no flows for configurable timeout
- Down: endpoint deleted or all flows have verdict=DROPPED

IMPORTANT: Hubble provides flow events (connection presence, verdict, protocol),
NOT bandwidth/byte-rate data. Metrics are flow-count based, not byte-rate based.

Uses compiled Hubble proto stubs (api/generated/) for native gRPC communication
with Hubble Relay — no hubble CLI binary needed.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import AsyncIterator, Callable, Optional

try:
    import grpc
    from grpc import aio as grpc_aio

    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

# Import generated Hubble proto stubs
try:
    import api.generated  # triggers sys.path setup for generated imports
    from observer import observer_pb2, observer_pb2_grpc
    from flow import flow_pb2

    PROTO_AVAILABLE = True
except ImportError:
    PROTO_AVAILABLE = False

logger = logging.getLogger(__name__)


class FlowVerdict(str, Enum):
    """Hubble flow verdict types."""

    FORWARDED = "FORWARDED"
    DROPPED = "DROPPED"
    ERROR = "ERROR"
    AUDIT = "AUDIT"
    REDIRECTED = "REDIRECTED"
    TRACED = "TRACED"
    TRANSLATED = "TRANSLATED"
    UNKNOWN = "UNKNOWN"


# Map protobuf verdict enum to our FlowVerdict
_VERDICT_MAP = {}
if PROTO_AVAILABLE:
    _VERDICT_MAP = {
        flow_pb2.FORWARDED: FlowVerdict.FORWARDED,
        flow_pb2.DROPPED: FlowVerdict.DROPPED,
        flow_pb2.ERROR: FlowVerdict.ERROR,
        flow_pb2.AUDIT: FlowVerdict.AUDIT,
        flow_pb2.REDIRECTED: FlowVerdict.REDIRECTED,
        flow_pb2.TRACED: FlowVerdict.TRACED,
        flow_pb2.TRANSLATED: FlowVerdict.TRANSLATED,
        flow_pb2.VERDICT_UNKNOWN: FlowVerdict.UNKNOWN,
    }


class TrafficDirection(str, Enum):
    """Traffic direction."""

    INGRESS = "INGRESS"
    EGRESS = "EGRESS"
    UNKNOWN = "UNKNOWN"


_DIRECTION_MAP = {}
if PROTO_AVAILABLE:
    _DIRECTION_MAP = {
        flow_pb2.INGRESS: TrafficDirection.INGRESS,
        flow_pb2.EGRESS: TrafficDirection.EGRESS,
        flow_pb2.TRAFFIC_DIRECTION_UNKNOWN: TrafficDirection.UNKNOWN,
    }


@dataclass
class Endpoint:
    """Represents a network endpoint (pod/service)."""

    namespace: str = ""
    pod_name: str = ""
    labels: list = field(default_factory=list)
    identity: int = 0
    ip: str = ""

    @property
    def id(self) -> str:
        if self.namespace and self.pod_name:
            return f"{self.namespace}/{self.pod_name}"
        return self.ip or f"identity:{self.identity}"

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "pod_name": self.pod_name,
            "labels": self.labels,
            "identity": self.identity,
            "ip": self.ip,
            "id": self.id,
        }


@dataclass
class FlowEvent:
    """Represents a network flow event from Hubble."""

    source: Endpoint
    destination: Endpoint
    verdict: FlowVerdict
    direction: TrafficDirection
    l4_protocol: str  # TCP, UDP, ICMP
    source_port: int = 0
    destination_port: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    drop_reason: str = ""
    is_reply: bool = False

    @property
    def flow_key(self) -> str:
        """Unique key for this flow pair."""
        return f"{self.source.id}->{self.destination.id}"

    def to_dict(self) -> dict:
        return {
            "source": self.source.to_dict(),
            "destination": self.destination.to_dict(),
            "verdict": self.verdict.value,
            "direction": self.direction.value,
            "l4_protocol": self.l4_protocol,
            "source_port": self.source_port,
            "destination_port": self.destination_port,
            "timestamp": self.timestamp.isoformat(),
            "drop_reason": self.drop_reason,
            "is_reply": self.is_reply,
            "flow_key": self.flow_key,
        }


class LinkState(str, Enum):
    """Link state derived from flow analysis."""

    ACTIVE = "active"
    IDLE = "idle"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class FlowMetrics:
    """
    Metrics derived from Hubble flow events.

    Hubble provides flow counts and connection metadata, NOT bandwidth.
    These are the real metrics Hubble can provide.
    """

    flows_total: int = 0
    flows_forwarded: int = 0
    flows_dropped: int = 0
    flows_per_second: float = 0.0
    active_connections: int = 0
    protocols: dict = field(default_factory=dict)
    last_flow_timestamp: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "flows_total": self.flows_total,
            "flows_forwarded": self.flows_forwarded,
            "flows_dropped": self.flows_dropped,
            "flows_per_second": round(self.flows_per_second, 2),
            "active_connections": self.active_connections,
            "protocols": self.protocols,
            "last_flow_timestamp": self.last_flow_timestamp.isoformat() if self.last_flow_timestamp else None,
        }


@dataclass
class LinkStateChange:
    """Represents a link state change event."""

    flow_key: str
    source: Endpoint
    destination: Endpoint
    old_state: LinkState
    new_state: LinkState
    metrics: Optional[FlowMetrics] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "flow_key": self.flow_key,
            "source": self.source.to_dict(),
            "destination": self.destination.to_dict(),
            "old_state": self.old_state.value,
            "new_state": self.new_state.value,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "timestamp": self.timestamp.isoformat(),
        }


class HubbleMonitor:
    """
    Monitor network flows via Hubble Relay gRPC API.

    Connects to Hubble Relay using compiled proto stubs and streams
    flow events to detect traffic activity between endpoints.

    NOTE: Hubble provides flow-level visibility (which connections exist,
    their verdict, protocol). It does NOT provide byte-rate bandwidth
    metrics. For bandwidth monitoring, use sysfs/netlink counters or
    a dedicated metrics pipeline (Prometheus + node_exporter).
    """

    def __init__(
        self,
        relay_addr: str = "hubble-relay:4245",
        idle_timeout_seconds: float = 5.0,
        callback: Optional[Callable[[LinkStateChange], None]] = None,
        use_tls: bool = False,
    ):
        if not GRPC_AVAILABLE:
            raise RuntimeError(
                "grpcio is required for Hubble monitoring. "
                "Install with: pip install grpcio"
            )
        if not PROTO_AVAILABLE:
            raise RuntimeError(
                "Hubble proto stubs not found in api/generated/. "
                "Regenerate with: uv run python -m grpc_tools.protoc ..."
            )

        self.relay_addr = relay_addr
        self.idle_timeout = timedelta(seconds=idle_timeout_seconds)
        self.callback = callback
        self.use_tls = use_tls

        self._channel: Optional[grpc_aio.Channel] = None
        self._stub: Optional[observer_pb2_grpc.ObserverStub] = None
        self._running = False

        # Flow tracking
        self._flow_last_seen: dict[str, datetime] = {}
        self._flow_states: dict[str, LinkState] = {}
        self._flow_endpoints: dict[str, tuple[Endpoint, Endpoint]] = {}
        self._flow_metrics: dict[str, FlowMetrics] = {}
        self._event_queue: asyncio.Queue[LinkStateChange] = asyncio.Queue()

        # Flow rate tracking
        self._flow_count_window: list[datetime] = []
        self._rate_window_seconds: float = 10.0

        self._idle_check_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect to Hubble Relay via gRPC."""
        logger.info(f"Connecting to Hubble Relay at {self.relay_addr}...")

        if self.use_tls:
            credentials = grpc.ssl_channel_credentials()
            self._channel = grpc_aio.secure_channel(self.relay_addr, credentials)
        else:
            self._channel = grpc_aio.insecure_channel(self.relay_addr)

        try:
            await asyncio.wait_for(self._channel.channel_ready(), timeout=10.0)
            logger.info("Connected to Hubble Relay")
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"Timeout connecting to Hubble Relay at {self.relay_addr}"
            )

        self._stub = observer_pb2_grpc.ObserverStub(self._channel)

    async def disconnect(self):
        """Disconnect from Hubble Relay."""
        self._stub = None
        if self._channel:
            await self._channel.close()
            self._channel = None
            logger.info("Disconnected from Hubble Relay")

    def _parse_endpoint(self, ep: "flow_pb2.Endpoint") -> Endpoint:
        """Parse Endpoint from protobuf message."""
        return Endpoint(
            namespace=ep.namespace,
            pod_name=ep.pod_name,
            labels=list(ep.labels),
            identity=ep.identity,
            ip="",  # IP is in the IP layer, not the endpoint
        )

    def _parse_flow(self, pb_flow: "flow_pb2.Flow") -> FlowEvent:
        """Parse FlowEvent from protobuf Flow message."""
        source = self._parse_endpoint(pb_flow.source)
        destination = self._parse_endpoint(pb_flow.destination)

        # Extract IP addresses from IP layer
        if pb_flow.HasField("IP"):
            source.ip = pb_flow.IP.source
            destination.ip = pb_flow.IP.destination

        # Parse L4 protocol and ports
        l4 = pb_flow.l4
        if l4.HasField("TCP"):
            l4_protocol = "TCP"
            src_port = l4.TCP.source_port
            dst_port = l4.TCP.destination_port
        elif l4.HasField("UDP"):
            l4_protocol = "UDP"
            src_port = l4.UDP.source_port
            dst_port = l4.UDP.destination_port
        elif l4.HasField("ICMPv4"):
            l4_protocol = "ICMPv4"
            src_port = 0
            dst_port = 0
        elif l4.HasField("ICMPv6"):
            l4_protocol = "ICMPv6"
            src_port = 0
            dst_port = 0
        elif l4.HasField("SCTP"):
            l4_protocol = "SCTP"
            src_port = l4.SCTP.source_port
            dst_port = l4.SCTP.destination_port
        else:
            l4_protocol = "UNKNOWN"
            src_port = 0
            dst_port = 0

        # Parse verdict
        verdict = _VERDICT_MAP.get(pb_flow.verdict, FlowVerdict.UNKNOWN)

        # Parse direction
        direction = _DIRECTION_MAP.get(pb_flow.traffic_direction, TrafficDirection.UNKNOWN)

        # Parse timestamp
        ts = datetime.now()
        if pb_flow.HasField("time"):
            ts = pb_flow.time.ToDatetime()

        # Parse is_reply
        is_reply = False
        if pb_flow.HasField("is_reply"):
            is_reply = pb_flow.is_reply.value

        # Drop reason description
        drop_reason = flow_pb2.DropReason.Name(pb_flow.drop_reason_desc) if pb_flow.drop_reason_desc else ""

        return FlowEvent(
            source=source,
            destination=destination,
            verdict=verdict,
            direction=direction,
            l4_protocol=l4_protocol,
            source_port=src_port,
            destination_port=dst_port,
            timestamp=ts,
            drop_reason=drop_reason,
            is_reply=is_reply,
        )

    def _update_flow_metrics(self, flow: FlowEvent) -> FlowMetrics:
        """Update and return flow metrics for a flow key."""
        flow_key = flow.flow_key
        now = datetime.now()

        if flow_key not in self._flow_metrics:
            self._flow_metrics[flow_key] = FlowMetrics()

        metrics = self._flow_metrics[flow_key]
        metrics.flows_total += 1
        metrics.last_flow_timestamp = now

        if flow.verdict == FlowVerdict.FORWARDED:
            metrics.flows_forwarded += 1
        elif flow.verdict == FlowVerdict.DROPPED:
            metrics.flows_dropped += 1

        # Track protocol breakdown
        metrics.protocols[flow.l4_protocol] = metrics.protocols.get(flow.l4_protocol, 0) + 1

        # Calculate flow rate over sliding window
        self._flow_count_window.append(now)
        cutoff = now - timedelta(seconds=self._rate_window_seconds)
        self._flow_count_window = [t for t in self._flow_count_window if t > cutoff]
        if len(self._flow_count_window) > 1:
            window_duration = (self._flow_count_window[-1] - self._flow_count_window[0]).total_seconds()
            if window_duration > 0:
                metrics.flows_per_second = len(self._flow_count_window) / window_duration

        # Count active connections
        metrics.active_connections = sum(
            1 for s in self._flow_states.values() if s == LinkState.ACTIVE
        )

        return metrics

    def _update_flow_state(self, flow: FlowEvent) -> Optional[LinkStateChange]:
        """Update flow state and return change event if state changed."""
        flow_key = flow.flow_key
        now = datetime.now()

        self._flow_endpoints[flow_key] = (flow.source, flow.destination)
        self._flow_last_seen[flow_key] = now

        old_state = self._flow_states.get(flow_key, LinkState.UNKNOWN)

        if flow.verdict == FlowVerdict.DROPPED:
            new_state = LinkState.DOWN
        elif flow.verdict == FlowVerdict.FORWARDED:
            new_state = LinkState.ACTIVE
        else:
            new_state = old_state

        # Update metrics
        metrics = self._update_flow_metrics(flow)

        if old_state != new_state:
            self._flow_states[flow_key] = new_state
            return LinkStateChange(
                flow_key=flow_key,
                source=flow.source,
                destination=flow.destination,
                old_state=old_state,
                new_state=new_state,
                metrics=metrics,
            )

        return None

    async def _check_idle_flows(self):
        """Periodically check for flows that have gone idle."""
        while self._running:
            try:
                await asyncio.sleep(1.0)

                now = datetime.now()
                idle_threshold = now - self.idle_timeout

                for flow_key, last_seen in list(self._flow_last_seen.items()):
                    if last_seen < idle_threshold:
                        old_state = self._flow_states.get(flow_key, LinkState.UNKNOWN)
                        if old_state == LinkState.ACTIVE:
                            self._flow_states[flow_key] = LinkState.IDLE

                            src, dst = self._flow_endpoints.get(
                                flow_key, (Endpoint(), Endpoint())
                            )
                            metrics = self._flow_metrics.get(flow_key)
                            if metrics:
                                metrics.flows_per_second = 0.0

                            change = LinkStateChange(
                                flow_key=flow_key,
                                source=src,
                                destination=dst,
                                old_state=old_state,
                                new_state=LinkState.IDLE,
                                metrics=metrics,
                            )
                            await self._event_queue.put(change)
                            if self.callback:
                                self.callback(change)

                            logger.info(f"Flow {flow_key} went idle")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in idle check: {e}")

    async def observe_flows(self) -> AsyncIterator[FlowEvent]:
        """
        Stream flows from Hubble Relay via native gRPC.

        Uses the compiled observer.proto stubs to call Observer.GetFlows
        with follow=True for continuous streaming.
        """
        if not self._stub:
            raise RuntimeError("Not connected to Hubble Relay. Call connect() first.")

        request = observer_pb2.GetFlowsRequest(follow=True)

        logger.info("Starting gRPC flow stream from Hubble Relay...")

        try:
            response_stream = self._stub.GetFlows(request)

            async for response in response_stream:
                if not self._running:
                    break

                # GetFlowsResponse has a oneof: flow, node_status, or lost_events
                if response.HasField("flow"):
                    try:
                        flow = self._parse_flow(response.flow)
                        yield flow
                    except Exception as e:
                        logger.error(f"Error parsing flow: {e}")
                        continue

                elif response.HasField("lost_events"):
                    lost = response.lost_events
                    logger.warning(
                        f"Lost {lost.num_events_lost} events "
                        f"(source: {flow_pb2.LostEventSource.Name(lost.source)})"
                    )

                elif response.HasField("node_status"):
                    ns = response.node_status
                    logger.debug(f"Node status: {ns.node_names}")

        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.CANCELLED:
                logger.info("Flow stream cancelled")
            elif e.code() == grpc.StatusCode.UNAVAILABLE:
                logger.error(
                    f"Hubble Relay unavailable at {self.relay_addr}. "
                    f"Verify the service is running and accessible."
                )
                raise
            else:
                logger.error(f"gRPC error: {e.code()} - {e.details()}")
                raise

    async def server_status(self) -> dict:
        """Get Hubble Relay server status."""
        if not self._stub:
            raise RuntimeError("Not connected")

        request = observer_pb2.ServerStatusRequest()
        response = await self._stub.ServerStatus(request)

        return {
            "num_flows": response.num_flows,
            "max_flows": response.max_flows,
            "seen_flows": response.seen_flows,
            "uptime_ns": response.uptime_ns,
            "flows_rate": response.flows_rate,
            "version": response.version,
            "num_connected_nodes": response.num_connected_nodes.value if response.HasField("num_connected_nodes") else 0,
            "num_unavailable_nodes": response.num_unavailable_nodes.value if response.HasField("num_unavailable_nodes") else 0,
            "unavailable_nodes": list(response.unavailable_nodes),
        }

    async def start(self):
        """Start monitoring flows."""
        if self._running:
            return

        logger.info("Starting Hubble monitor...")

        await self.connect()
        self._running = True

        # Log server status
        try:
            status = await self.server_status()
            logger.info(
                f"Hubble Relay status: version={status['version']}, "
                f"flows_rate={status['flows_rate']:.1f}/s, "
                f"connected_nodes={status['num_connected_nodes']}"
            )
        except Exception as e:
            logger.warning(f"Could not get Hubble server status: {e}")

        self._idle_check_task = asyncio.create_task(self._check_idle_flows())

        logger.info("Hubble monitor started")

    async def stop(self):
        """Stop monitoring."""
        logger.info("Stopping Hubble monitor...")
        self._running = False

        if self._idle_check_task:
            self._idle_check_task.cancel()
            try:
                await self._idle_check_task
            except asyncio.CancelledError:
                pass

        await self.disconnect()
        logger.info("Hubble monitor stopped")

    async def events(self) -> AsyncIterator[LinkStateChange]:
        """Async iterator for link state change events."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue

    async def run(self):
        """Main loop: observe flows and emit state changes."""
        await self.start()

        try:
            async for flow in self.observe_flows():
                change = self._update_flow_state(flow)
                if change:
                    await self._event_queue.put(change)
                    if self.callback:
                        self.callback(change)
                    logger.info(
                        f"Flow state change: {change.flow_key} "
                        f"{change.old_state.value} -> {change.new_state.value}"
                    )

        except Exception as e:
            logger.error(f"Error in flow observation: {e}")
        finally:
            await self.stop()

    def get_flow_states(self) -> dict[str, LinkState]:
        """Get current state of all tracked flows."""
        return self._flow_states.copy()

    def get_active_flows(self) -> list[str]:
        """Get list of currently active flow keys."""
        return [k for k, v in self._flow_states.items() if v == LinkState.ACTIVE]

    def get_flow_metrics(self, flow_key: str) -> Optional[FlowMetrics]:
        """Get metrics for a specific flow."""
        return self._flow_metrics.get(flow_key)

    def get_all_flow_metrics(self) -> dict[str, FlowMetrics]:
        """Get metrics for all tracked flows."""
        return self._flow_metrics.copy()

    @property
    def is_running(self) -> bool:
        return self._running
