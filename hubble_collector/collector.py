"""
Hubble flow collector for Clabernetes topology nodes.

Connects to Hubble Relay, subscribes to all flows, and resolves each
flow to a specific topology link using:
  1. flow.tunnel.vni  → Connectivity CR → (namespace, topology, node, iface)
  2. Fallback: src/dst pod labels → NodeInfo → link lookup by pod pair

Per-link metrics written to Redis every FLUSH_INTERVAL_S seconds:
  nm:topo:{ns}:{topo}:{node}:{iface}  (Stream)

Fields written (matching the sidecar schema where possible):
  rx_bps, tx_bps        0.0  (Hubble does not provide byte rates)
  rx_pps, tx_pps        0.0
  rx_bytes_total        0
  tx_bytes_total        0
  rx_packets_total      0
  tx_packets_total      0
  rx_errors             0
  tx_errors             0
  rx_dropped            0
  tx_dropped            0
  state                 active | idle | down
  flows_fwd             forwarded flow count since last flush
  flows_dropped         dropped flow count since last flush
  flows_per_second      rate over flush window
  source                hubble

Environment variables:
  HUBBLE_RELAY_ADDR     host:port of Hubble Relay (default: hubble-relay.kube-system.svc:4245)
  HUBBLE_TLS            true/false — enable TLS (default: false)
  HUBBLE_TLS_SECRET     K8s secret name override (default: auto-discover)
  CILIUM_NAMESPACE      namespace where Cilium/Hubble is installed (default: kube-system)
  WATCH_NAMESPACE       Clabernetes namespace to watch (default: all namespaces)
  TOPOLOGY_REFRESH_S    how often to refresh topology from K8s (default: 30)
  FLUSH_INTERVAL_S      how often to flush metrics to Redis (default: 5)
  IDLE_TIMEOUT_S        seconds without flows before link marked idle (default: 10)
  REDIS_URL             Redis URL (optional; if unset writes skipped)
  REDIS_STREAM_MAXLEN   max entries per stream (default: 43200)
  LOG_LEVEL             logging level (default: INFO)
"""

import logging
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import grpc
from grpc import aio as grpc_aio

# Reuse generated proto stubs from the api/ tree
_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "api" / "generated"))

from observer import observer_pb2, observer_pb2_grpc  # noqa: E402
from flow import flow_pb2  # noqa: E402

from .tls import gather_tls_certs, build_grpc_credentials  # noqa: E402
from .topology import TopologyResolver, LinkEndpoints  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HUBBLE_RELAY_ADDR = os.environ.get(
    "HUBBLE_RELAY_ADDR", "hubble-relay.kube-system.svc.cluster.local:4245"
)
WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE") or None
TOPOLOGY_REFRESH_S = int(os.environ.get("TOPOLOGY_REFRESH_S", "30"))
FLUSH_INTERVAL_S = int(os.environ.get("FLUSH_INTERVAL_S", "5"))
IDLE_TIMEOUT_S = int(os.environ.get("IDLE_TIMEOUT_S", "10"))
REDIS_URL = os.environ.get("REDIS_URL")
STREAM_MAXLEN = int(os.environ.get("REDIS_STREAM_MAXLEN", "43200"))
KEY_PREFIX = "nm"

# ---------------------------------------------------------------------------
# Per-link accumulator
# ---------------------------------------------------------------------------


@dataclass
class LinkAccumulator:
    """Accumulates flow events for one link direction between flushes."""
    flows_fwd: int = 0
    flows_dropped: int = 0
    last_flow_ts: float = field(default_factory=time.monotonic)
    window_start: float = field(default_factory=time.monotonic)

    def record(self, forwarded: bool):
        now = time.monotonic()
        if forwarded:
            self.flows_fwd += 1
        else:
            self.flows_dropped += 1
        self.last_flow_ts = now

    def flush(self) -> dict:
        """Return metrics dict and reset counters."""
        now = time.monotonic()
        elapsed = max(now - self.window_start, 0.001)
        total = self.flows_fwd + self.flows_dropped
        fps = total / elapsed

        age = now - self.last_flow_ts
        if self.flows_fwd == 0 and self.flows_dropped == 0:
            state = "idle"
        elif age > IDLE_TIMEOUT_S:
            state = "idle"
        elif self.flows_dropped > 0 and self.flows_fwd == 0:
            state = "down"
        else:
            state = "active"

        metrics = {
            # Zero-valued sidecar-compatible fields
            "rx_bps": "0.0",
            "tx_bps": "0.0",
            "rx_pps": "0.0",
            "tx_pps": "0.0",
            "rx_bytes_total": "0",
            "tx_bytes_total": "0",
            "rx_packets_total": "0",
            "tx_packets_total": "0",
            "rx_errors": "0",
            "tx_errors": "0",
            "rx_dropped": "0",
            "tx_dropped": "0",
            # Hubble-specific
            "flows_fwd": str(self.flows_fwd),
            "flows_dropped": str(self.flows_dropped),
            "flows_per_second": f"{fps:.2f}",
            "state": state,
            "source": "hubble",
        }

        self.flows_fwd = 0
        self.flows_dropped = 0
        self.window_start = now
        return metrics


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def connect_redis(redis_url: Optional[str]):
    if not redis_url:
        return None
    try:
        import redis as redislib
        client = redislib.from_url(redis_url, socket_connect_timeout=5)
        client.ping()
        logger.info("Redis connected: %s", redis_url)
        return client
    except Exception as exc:
        logger.warning("Redis unavailable (%s), continuing without persistence", exc)
        return None


def flush_to_redis(redis_client, accumulators: dict, topo: TopologyResolver):
    """
    Flush all link accumulators to Redis Streams.

    accumulator key: (namespace, topology, node, iface)
    """
    if redis_client is None or not accumulators:
        return

    try:
        pipe = redis_client.pipeline(transaction=False)
        for (ns, topo_name, node, iface), acc in accumulators.items():
            fields = acc.flush()
            topo_key = f"{KEY_PREFIX}:topo:{ns}:{topo_name}"
            stream_key = f"{topo_key}:{node}:{iface}"
            pipe.xadd(stream_key, fields, maxlen=STREAM_MAXLEN, approximate=True)
            pipe.sadd(f"{topo_key}:{node}:ifaces", iface)
            pipe.sadd(f"{topo_key}:nodes", node)
            pipe.sadd(f"{KEY_PREFIX}:topologies", f"{ns}/{topo_name}")
        pipe.execute()
    except Exception as exc:
        logger.warning("Redis flush failed: %s", exc)


# ---------------------------------------------------------------------------
# Flow processing
# ---------------------------------------------------------------------------


def _is_forwarded(verdict: int) -> bool:
    return verdict == flow_pb2.FORWARDED


def _extract_pod_name(endpoint) -> str:
    return endpoint.pod_name if endpoint else ""


def process_flow(
    flow,
    topo: TopologyResolver,
    accumulators: dict,
    lock: threading.Lock,
):
    """
    Resolve a Hubble flow to topology link endpoints and update accumulators.

    Resolution strategy:
      1. Use flow.tunnel.vni to look up the exact link via Connectivity CR
      2. Fallback: use src/dst pod name → NodeInfo, find links by pod pair
    """
    forwarded = _is_forwarded(flow.verdict)

    # --- Strategy 1: VNI-based resolution ---
    vni = 0
    if flow.HasField("tunnel"):
        vni = flow.tunnel.vni

    if vni:
        link = topo.resolve_vni(vni)
        if link:
            _record_link(link, forwarded, accumulators, lock, egress=True)
            return

    # --- Strategy 2: Pod-pair fallback ---
    src_pod = _extract_pod_name(flow.source)
    dst_pod = _extract_pod_name(flow.destination)

    if not src_pod or not dst_pod:
        return

    src_node = topo.resolve_pod(src_pod)
    dst_node = topo.resolve_pod(dst_pod)

    if not src_node or not dst_node:
        return

    # Find any link between these two nodes
    for link in topo._vni_map.values():
        local_matches = (
            link.local.node == src_node.node
            and link.remote.node == dst_node.node
            and link.local.namespace == src_node.namespace
        )
        remote_matches = (
            link.remote.node == src_node.node
            and link.local.node == dst_node.node
            and link.local.namespace == src_node.namespace
        )
        if local_matches or remote_matches:
            egress = local_matches
            _record_link(link, forwarded, accumulators, lock, egress=egress)
            # Note: if multiple links exist between the same pair, all are
            # attributed equally (pod-pair fallback is inherently imprecise)


def _record_link(
    link: LinkEndpoints,
    forwarded: bool,
    accumulators: dict,
    lock: threading.Lock,
    egress: bool,
):
    """Record one flow event against both sides of the link."""
    # egress=True means flow is going FROM local TO remote
    local = link.local
    remote = link.remote

    local_key = (local.namespace, local.topology, local.node, local.redis_iface)
    remote_key = (remote.namespace, remote.topology, remote.node, remote.redis_iface)

    with lock:
        if local_key not in accumulators:
            accumulators[local_key] = LinkAccumulator()
        if remote_key not in accumulators:
            accumulators[remote_key] = LinkAccumulator()

        accumulators[local_key].record(forwarded)
        accumulators[remote_key].record(forwarded)


# ---------------------------------------------------------------------------
# Topology refresh thread
# ---------------------------------------------------------------------------


def topology_refresh_loop(topo: TopologyResolver, stop_event: threading.Event):
    """Periodically refresh topology from K8s in a background thread."""
    while not stop_event.is_set():
        try:
            topo.refresh()
        except Exception as exc:
            logger.warning("Topology refresh failed: %s", exc)
        stop_event.wait(TOPOLOGY_REFRESH_S)


# ---------------------------------------------------------------------------
# Redis flush thread
# ---------------------------------------------------------------------------


def redis_flush_loop(
    redis_client,
    accumulators: dict,
    lock: threading.Lock,
    topo: TopologyResolver,
    stop_event: threading.Event,
):
    """Periodically flush accumulators to Redis in a background thread."""
    while not stop_event.is_set():
        stop_event.wait(FLUSH_INTERVAL_S)
        if not accumulators:
            continue
        with lock:
            snap = dict(accumulators)
        flush_to_redis(redis_client, snap, topo)


# ---------------------------------------------------------------------------
# Main gRPC flow loop (async)
# ---------------------------------------------------------------------------


async def run_collector(topo: TopologyResolver, redis_client, stop_event: threading.Event):
    """Open gRPC connection to Hubble Relay and stream flows."""
    tls = gather_tls_certs()

    if tls:
        creds = build_grpc_credentials(tls)
        channel = grpc_aio.secure_channel(HUBBLE_RELAY_ADDR, creds)
        logger.info("Connecting to Hubble Relay (TLS) at %s", HUBBLE_RELAY_ADDR)
    else:
        channel = grpc_aio.insecure_channel(HUBBLE_RELAY_ADDR)
        logger.info("Connecting to Hubble Relay (plaintext) at %s", HUBBLE_RELAY_ADDR)

    accumulators: dict = {}
    lock = threading.Lock()

    # Start Redis flush thread
    flush_thread = threading.Thread(
        target=redis_flush_loop,
        args=(redis_client, accumulators, lock, topo, stop_event),
        daemon=True,
    )
    flush_thread.start()

    stub = observer_pb2_grpc.ObserverStub(channel)

    request = observer_pb2.GetFlowsRequest(follow=True)

    reconnect_delay = 5
    while not stop_event.is_set():
        try:
            logger.info("Subscribing to Hubble flow stream...")
            async for response in stub.GetFlows(request):
                if stop_event.is_set():
                    break
                if not response.HasField("flow"):
                    continue
                process_flow(response.flow, topo, accumulators, lock)

        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.CANCELLED:
                break
            logger.error(
                "gRPC error: %s %s — reconnecting in %ds",
                exc.code(), exc.details(), reconnect_delay,
            )
            stop_event.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        except Exception as exc:
            logger.error("Unexpected error: %s — reconnecting in %ds", exc, reconnect_delay)
            stop_event.wait(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        else:
            reconnect_delay = 5  # reset on clean stream end

    await channel.close()
    logger.info("Hubble collector stopped")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import asyncio
    import signal

    logger.info("Starting Hubble flow collector")
    logger.info("  relay:     %s", HUBBLE_RELAY_ADDR)
    logger.info("  namespace: %s", WATCH_NAMESPACE or "all")
    logger.info("  refresh:   %ds", TOPOLOGY_REFRESH_S)
    logger.info("  flush:     %ds", FLUSH_INTERVAL_S)
    logger.info("  idle:      %ds", IDLE_TIMEOUT_S)
    logger.info("  redis:     %s", "enabled" if REDIS_URL else "disabled")

    topo = TopologyResolver(namespace=WATCH_NAMESPACE)
    try:
        topo.refresh()
    except Exception as exc:
        logger.error("Initial topology load failed: %s", exc)

    redis_client = connect_redis(REDIS_URL)

    stop_event = threading.Event()

    # Topology refresh background thread
    refresh_thread = threading.Thread(
        target=topology_refresh_loop,
        args=(topo, stop_event),
        daemon=True,
    )
    refresh_thread.start()

    def _shutdown(sig, frame):
        logger.info("Shutting down (signal %s)...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    asyncio.run(run_collector(topo, redis_client, stop_event))


if __name__ == "__main__":
    main()
