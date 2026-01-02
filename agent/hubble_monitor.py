"""
Hubble Monitor - Network flow monitoring via Cilium Hubble Relay gRPC API.

Provides real-time visibility into L3/L4 network flows by connecting to
Hubble Relay and streaming flow events. Detects:
- Active traffic: flows observed for endpoint pair
- Idle: no flows for configurable timeout
- Down: endpoint deleted or all flows have verdict=DROPPED
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


class TrafficDirection(str, Enum):
    """Traffic direction."""
    INGRESS = "INGRESS"
    EGRESS = "EGRESS"
    UNKNOWN = "UNKNOWN"


@dataclass
class Endpoint:
    """Represents a network endpoint (pod/service)."""
    namespace: str = ""
    pod_name: str = ""
    labels: dict = field(default_factory=dict)
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
    bytes: int = 0
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
            "bytes": self.bytes,
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
class LinkStateChange:
    """Represents a link state change event."""
    flow_key: str
    source: Endpoint
    destination: Endpoint
    old_state: LinkState
    new_state: LinkState
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "flow_key": self.flow_key,
            "source": self.source.to_dict(),
            "destination": self.destination.to_dict(),
            "old_state": self.old_state.value,
            "new_state": self.new_state.value,
            "timestamp": self.timestamp.isoformat(),
        }


class HubbleMonitor:
    """
    Monitor network flows via Hubble Relay gRPC API.

    Connects to Hubble Relay and streams flow events to detect
    traffic activity between endpoints.
    """

    # Hubble Observer service proto (simplified inline definition)
    # In production, generate from: https://github.com/cilium/cilium/tree/main/api/v1/observer
    OBSERVER_PROTO = """
    syntax = "proto3";
    package observer;

    service Observer {
        rpc GetFlows(GetFlowsRequest) returns (stream GetFlowsResponse);
        rpc ServerStatus(ServerStatusRequest) returns (ServerStatusResponse);
    }
    """

    def __init__(
        self,
        relay_addr: str = "hubble-relay:4245",
        idle_timeout_seconds: float = 5.0,
        callback: Optional[Callable[[LinkStateChange], None]] = None,
    ):
        """
        Initialize Hubble monitor.

        Args:
            relay_addr: Hubble Relay gRPC address (host:port)
            idle_timeout_seconds: Seconds without flows before marking idle
            callback: Optional callback for state changes
        """
        if not GRPC_AVAILABLE:
            raise RuntimeError("grpcio is required for Hubble monitoring. Install with: pip install grpcio")

        self.relay_addr = relay_addr
        self.idle_timeout = timedelta(seconds=idle_timeout_seconds)
        self.callback = callback

        self._channel: Optional[grpc_aio.Channel] = None
        self._running = False

        # Flow tracking
        self._flow_last_seen: dict[str, datetime] = {}  # flow_key -> last_seen
        self._flow_states: dict[str, LinkState] = {}  # flow_key -> current_state
        self._flow_endpoints: dict[str, tuple[Endpoint, Endpoint]] = {}  # flow_key -> (src, dst)
        self._event_queue: asyncio.Queue[LinkStateChange] = asyncio.Queue()

        # Idle detection task
        self._idle_check_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect to Hubble Relay."""
        logger.info(f"Connecting to Hubble Relay at {self.relay_addr}...")

        self._channel = grpc_aio.insecure_channel(self.relay_addr)

        # Wait for channel to be ready
        try:
            await asyncio.wait_for(
                self._channel.channel_ready(),
                timeout=10.0
            )
            logger.info("Connected to Hubble Relay")
        except asyncio.TimeoutError:
            raise ConnectionError(f"Timeout connecting to Hubble Relay at {self.relay_addr}")

    async def disconnect(self):
        """Disconnect from Hubble Relay."""
        if self._channel:
            await self._channel.close()
            self._channel = None
            logger.info("Disconnected from Hubble Relay")

    def _parse_endpoint(self, endpoint_data: dict) -> Endpoint:
        """Parse endpoint from Hubble flow data."""
        return Endpoint(
            namespace=endpoint_data.get("namespace", ""),
            pod_name=endpoint_data.get("pod_name", ""),
            labels=endpoint_data.get("labels", {}),
            identity=endpoint_data.get("identity", 0),
            ip=endpoint_data.get("ip", ""),
        )

    def _parse_verdict(self, verdict: str) -> FlowVerdict:
        """Parse flow verdict."""
        try:
            return FlowVerdict(verdict.upper())
        except ValueError:
            return FlowVerdict.UNKNOWN

    def _parse_flow(self, flow_data: dict) -> FlowEvent:
        """Parse flow from Hubble response."""
        source = self._parse_endpoint(flow_data.get("source", {}))
        destination = self._parse_endpoint(flow_data.get("destination", {}))

        l4 = flow_data.get("l4", {})
        if "TCP" in l4:
            l4_protocol = "TCP"
            src_port = l4["TCP"].get("source_port", 0)
            dst_port = l4["TCP"].get("destination_port", 0)
        elif "UDP" in l4:
            l4_protocol = "UDP"
            src_port = l4["UDP"].get("source_port", 0)
            dst_port = l4["UDP"].get("destination_port", 0)
        elif "ICMPv4" in l4 or "ICMPv6" in l4:
            l4_protocol = "ICMP"
            src_port = 0
            dst_port = 0
        else:
            l4_protocol = "UNKNOWN"
            src_port = 0
            dst_port = 0

        return FlowEvent(
            source=source,
            destination=destination,
            verdict=self._parse_verdict(flow_data.get("verdict", "UNKNOWN")),
            direction=TrafficDirection(flow_data.get("traffic_direction", "UNKNOWN")),
            l4_protocol=l4_protocol,
            source_port=src_port,
            destination_port=dst_port,
            bytes=flow_data.get("l7", {}).get("bytes", 0),
            drop_reason=flow_data.get("drop_reason_desc", ""),
            is_reply=flow_data.get("is_reply", False),
        )

    def _update_flow_state(self, flow: FlowEvent) -> Optional[LinkStateChange]:
        """Update flow state and return change event if state changed."""
        flow_key = flow.flow_key
        now = datetime.now()

        # Store endpoint info
        self._flow_endpoints[flow_key] = (flow.source, flow.destination)
        self._flow_last_seen[flow_key] = now

        old_state = self._flow_states.get(flow_key, LinkState.UNKNOWN)

        # Determine new state based on verdict
        if flow.verdict == FlowVerdict.DROPPED:
            new_state = LinkState.DOWN
        elif flow.verdict == FlowVerdict.FORWARDED:
            new_state = LinkState.ACTIVE
        else:
            new_state = old_state  # Keep current state for other verdicts

        if old_state != new_state:
            self._flow_states[flow_key] = new_state
            return LinkStateChange(
                flow_key=flow_key,
                source=flow.source,
                destination=flow.destination,
                old_state=old_state,
                new_state=new_state,
            )

        return None

    async def _check_idle_flows(self):
        """Periodically check for flows that have gone idle."""
        while self._running:
            try:
                await asyncio.sleep(1.0)  # Check every second

                now = datetime.now()
                idle_threshold = now - self.idle_timeout

                for flow_key, last_seen in list(self._flow_last_seen.items()):
                    if last_seen < idle_threshold:
                        old_state = self._flow_states.get(flow_key, LinkState.UNKNOWN)
                        if old_state == LinkState.ACTIVE:
                            self._flow_states[flow_key] = LinkState.IDLE

                            src, dst = self._flow_endpoints.get(flow_key, (Endpoint(), Endpoint()))
                            change = LinkStateChange(
                                flow_key=flow_key,
                                source=src,
                                destination=dst,
                                old_state=old_state,
                                new_state=LinkState.IDLE,
                            )
                            await self._event_queue.put(change)
                            if self.callback:
                                self.callback(change)

                            logger.info(f"Flow {flow_key} went idle")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in idle check: {e}")

    async def _observe_flows_grpc(self) -> AsyncIterator[FlowEvent]:
        """
        Stream flows from Hubble Relay via gRPC.

        Note: This is a simplified implementation. In production, use
        generated protobuf stubs from Cilium repo.
        """
        if not self._channel:
            await self.connect()

        # Create stub for Observer service
        # In production, use: observer_pb2_grpc.ObserverStub(self._channel)
        # Here we use reflection or dynamic stub

        logger.info("Starting flow observation...")

        # For now, yield a placeholder - actual implementation requires
        # compiled protobuf stubs from Cilium
        logger.warning(
            "Hubble gRPC observation requires compiled protobuf stubs. "
            "See: https://github.com/cilium/cilium/tree/main/api/v1/observer"
        )

        # Simulate flow stream for testing
        while self._running:
            await asyncio.sleep(1.0)
            # In production, this would yield actual flows from gRPC stream

    async def observe_flows_http(self) -> AsyncIterator[FlowEvent]:
        """
        Alternative: Stream flows via Hubble CLI subprocess.

        Uses `hubble observe --output json` as a fallback when
        gRPC stubs are not available.
        """
        import subprocess
        import json

        cmd = [
            "hubble", "observe",
            "--server", self.relay_addr,
            "--output", "json",
            "--follow",
        ]

        logger.info(f"Starting Hubble CLI: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            while self._running:
                line = await process.stdout.readline()
                if not line:
                    break

                try:
                    flow_data = json.loads(line.decode())
                    flow = self._parse_flow(flow_data.get("flow", {}))
                    yield flow
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"Error parsing flow: {e}")

        finally:
            process.terminate()
            await process.wait()

    async def start(self):
        """Start monitoring flows."""
        if self._running:
            return

        logger.info("Starting Hubble monitor...")

        await self.connect()
        self._running = True

        # Start idle detection
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
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    async def run(self):
        """Main loop: observe flows and emit state changes."""
        await self.start()

        try:
            # Try HTTP/CLI method as fallback
            async for flow in self.observe_flows_http():
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

    @property
    def is_running(self) -> bool:
        return self._running


# Standalone usage example
async def main():
    """Example usage of HubbleMonitor."""
    def on_change(event: LinkStateChange):
        print(f"[{event.timestamp}] {event.flow_key}: {event.old_state.value} -> {event.new_state.value}")

    monitor = HubbleMonitor(
        relay_addr="hubble-relay.kube-system.svc:4245",
        idle_timeout_seconds=5.0,
        callback=on_change,
    )

    try:
        await monitor.run()
    except KeyboardInterrupt:
        pass
    finally:
        await monitor.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
