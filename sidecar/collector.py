"""
Network Monitor standalone collector.

Fallback deployment for environments where sidecar injection is not possible.
Discovers Clabernetes launcher pods via kubectl, reads /proc/net/dev from each
pod, computes rates, and writes to Redis Streams + serves via HTTP.

Environment variables:
    NAMESPACE:          Namespace to watch for clab pods (default: clab)
    POLL_INTERVAL_MS:   Polling interval in milliseconds (default: 2000)
    POD_SELECTOR:       Label selector for target pods (default: clabernetes/app=clabernetes)
    EXCLUDE_IFACES:     Comma-separated interface names to skip (default: lo)
    API_PORT:           HTTP server port (default: 9000)
    REDIS_URL:          Redis connection URL (optional)
    REDIS_STREAM_MAXLEN: Max entries per interface stream (default: 43200)
    LOG_LEVEL:          Logging level (default: INFO)
"""

import json
import logging
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from .common import compute_rates, write_to_redis
except ImportError:
    from common import compute_rates, write_to_redis

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] collector: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("collector")

# ---------------------------------------------------------------------------
# Shared metrics store: {node_id: {"interfaces": [...], "topology": ..., ...}}
# ---------------------------------------------------------------------------

nodes_store: dict = {}
nodes_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pod discovery
# ---------------------------------------------------------------------------

# Clabernetes labels on launcher pods
_LABEL_TOPOLOGY = "clabernetes/topologyOwner"
_LABEL_NODE = "clabernetes/topologyNode"


def get_pods(namespace: str, selector: str) -> list[dict]:
    """
    List target pods via kubectl.

    Returns list of dicts with keys: name, namespace, topology, node.
    topology and node come from Clabernetes labels (empty string if absent).
    """
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", namespace,
                "-l", selector,
                "--field-selector=status.phase=Running",
                "-o", (
                    "jsonpath={range .items[*]}"
                    "{.metadata.name} {.metadata.namespace}"
                    " {.metadata.labels.clabernetes/topologyOwner}"
                    " {.metadata.labels.clabernetes/topologyNode}"
                    "{\"\\n\"}{end}"
                ),
            ],
            capture_output=True, text=True, timeout=10,
        )
        pods = []
        raw = result.stdout.strip().replace("\\n", "\n")
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                pods.append({
                    "name": parts[0],
                    "namespace": parts[1],
                    "topology": parts[2] if len(parts) > 2 else "",
                    "node": parts[3] if len(parts) > 3 else "",
                })
        return pods
    except Exception as exc:
        logger.error("Failed to list pods: %s", exc)
        return []


# ---------------------------------------------------------------------------
# /proc/net/dev reader
# ---------------------------------------------------------------------------


def read_proc_net_dev(pod_name: str, namespace: str) -> dict:
    """
    Read /proc/net/dev from a pod via kubectl exec.

    Returns {iface: {rx_bytes, tx_bytes, rx_packets, tx_packets,
                     rx_errors, tx_errors, rx_dropped, tx_dropped}}
    """
    try:
        result = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod_name, "--", "cat", "/proc/net/dev"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.debug("Failed to read /proc/net/dev from %s: %s",
                         pod_name, result.stderr.strip())
            return {}

        interfaces = {}
        for line in result.stdout.strip().split("\n")[2:]:  # Skip 2 header lines
            line = line.strip()
            if not line or ":" not in line:
                continue
            iface, stats = line.split(":", 1)
            iface = iface.strip()
            values = stats.split()
            if len(values) < 16:
                continue

            interfaces[iface] = {
                "rx_bytes": int(values[0]),
                "rx_packets": int(values[1]),
                "rx_errors": int(values[2]),
                "rx_dropped": int(values[3]),
                "tx_bytes": int(values[8]),
                "tx_packets": int(values[9]),
                "tx_errors": int(values[10]),
                "tx_dropped": int(values[11]),
            }
        return interfaces
    except Exception as exc:
        logger.debug("Error reading from %s: %s", pod_name, exc)
        return {}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def make_handler(store: dict | None = None, lock: threading.Lock | None = None):
    _store = store if store is not None else nodes_store
    _lock = lock if lock is not None else nodes_lock

    class CollectorHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/interfaces", "/metrics"):
                with _lock:
                    data = list(_store.values())
                body = json.dumps({"nodes": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    return CollectorHandler


# ---------------------------------------------------------------------------
# Redis connection
# ---------------------------------------------------------------------------


def connect_redis(redis_url: str | None):
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    namespace = os.environ.get("NAMESPACE", "clab")
    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    pod_selector = os.environ.get("POD_SELECTOR", "clabernetes/app=clabernetes")
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}
    api_port = int(os.environ.get("API_PORT", "9000"))
    redis_url = os.environ.get("REDIS_URL")

    poll_interval_s = poll_interval_ms / 1000.0

    logger.info("Starting standalone collector")
    logger.info("  namespace: %s", namespace)
    logger.info("  selector:  %s", pod_selector)
    logger.info("  poll:      %dms", poll_interval_ms)
    logger.info("  http port: %d", api_port)
    logger.info("  redis:     %s", "enabled" if redis_url else "disabled")

    redis_client = connect_redis(redis_url)

    # HTTP server
    server = HTTPServer(("0.0.0.0", api_port), make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("HTTP server listening on port %d", api_port)

    prev_state: dict[str, dict] = {}  # {node_id: {iface: counters}}

    try:
        while True:
            pods = get_pods(namespace, pod_selector)
            if not pods:
                logger.warning("No running pods found in %s with selector %s",
                               namespace, pod_selector)
                time.sleep(poll_interval_s)
                continue

            logger.debug("Found %d pods", len(pods))
            for pod in pods:
                pod_namespace = pod["namespace"]
                pod_name = pod["name"]
                topology = pod.get("topology") or pod_namespace
                node = pod.get("node") or pod_name
                node_id = f"{pod_namespace}/{topology}/{node}"

                curr_counters = read_proc_net_dev(pod_name, pod_namespace)
                if not curr_counters:
                    continue

                prev_counters = prev_state.get(node_id, {})
                metrics = compute_rates(prev_counters, curr_counters, poll_interval_s, exclude)

                if metrics:
                    with nodes_lock:
                        nodes_store[node_id] = {
                            "node_id": node_id,
                            "interfaces": metrics,
                        }
                    write_to_redis(redis_client, pod_namespace, topology, node, metrics)
                    logger.debug("Updated %d interfaces for %s", len(metrics), node_id)

                prev_state[node_id] = curr_counters

            time.sleep(poll_interval_s)

    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
