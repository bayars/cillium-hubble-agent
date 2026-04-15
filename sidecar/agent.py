"""
Network Monitor sidecar agent.

Reads per-interface rx/tx counters from /sys/class/net/*/statistics/,
serves current metrics via a GET HTTP endpoint, and writes time-series
data to Redis Streams (optional — if REDIS_URL is unset or Redis is
unavailable, the HTTP endpoint continues to work from in-memory store).

Runs as a sidecar container (via Clabernetes extraContainers) sharing
the pod's network namespace, so it sees all interfaces including
linecards, CPM, and mgmt interfaces.

Node ID auto-detection (in priority order):
    1. NODE_ID env var (explicit override)
    2. POD_NAME + POD_NAMESPACE env vars (Kubernetes downward API)

Environment variables:
    TOPOLOGY_NAME:      Clabernetes topology name (from label clabernetes/topologyOwner)
    NODE_NAME:          Node name within the topology (from label clabernetes/topologyNode)
    POD_NAMESPACE:      Pod namespace from Kubernetes downward API
    POLL_INTERVAL_MS:   Polling interval in milliseconds (default: 2000)
    EXCLUDE_IFACES:     Comma-separated interface names to skip (default: lo)
    API_PORT:           Port for the sidecar HTTP server (default: 9000)
    REDIS_URL:          Redis URL, e.g. redis://:pass@host:6379/0 (optional)
    REDIS_STREAM_MAXLEN: Max entries per interface stream (default: 43200)

node_id is built as "{namespace}/{topology}/{node}" — stable across pod restarts
and unique across topologies. Example: "default/srl-probe-test/srl1".
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from .common import compute_rates, write_to_redis
except ImportError:
    from common import compute_rates, write_to_redis

SYSFS_NET = Path("/sys/class/net")

# Shared in-memory store updated by the poll loop, read by the HTTP server.
metrics_store: dict = {}
metrics_lock = threading.Lock()

# Resolved at startup, used by the HTTP handler.
node_id: str = ""


def read_counter(iface_path: Path, counter: str) -> int:
    try:
        return int((iface_path / "statistics" / counter).read_text().strip())
    except (OSError, ValueError):
        return 0


def get_operstate(iface_path: Path) -> str:
    try:
        state = (iface_path / "operstate").read_text().strip()
        return "up" if state == "up" else "down"
    except OSError:
        return "unknown"


def discover_interfaces(exclude: set[str]) -> list[str]:
    """List all network interfaces except excluded ones."""
    if not SYSFS_NET.exists():
        print(f"[ERROR] {SYSFS_NET} does not exist", flush=True)
        return []
    return [
        d.name
        for d in SYSFS_NET.iterdir()
        if d.is_symlink() or d.is_dir()
        if d.name not in exclude
        if (d / "statistics").exists()
    ]


def read_all_counters(interfaces: list[str]) -> dict:
    """Read all counters for all interfaces. Returns {iface: {counter: value}}."""
    result = {}
    for iface in interfaces:
        iface_path = SYSFS_NET / iface
        result[iface] = {
            "rx_bytes": read_counter(iface_path, "rx_bytes"),
            "tx_bytes": read_counter(iface_path, "tx_bytes"),
            "rx_packets": read_counter(iface_path, "rx_packets"),
            "tx_packets": read_counter(iface_path, "tx_packets"),
            "rx_errors": read_counter(iface_path, "rx_errors"),
            "tx_errors": read_counter(iface_path, "tx_errors"),
            "rx_dropped": read_counter(iface_path, "rx_dropped"),
            "tx_dropped": read_counter(iface_path, "tx_dropped"),
            "operstate": get_operstate(iface_path),
        }
    return result


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/interfaces", "/metrics"):
            with metrics_lock:
                data = list(metrics_store.values())
            body = json.dumps({"node_id": node_id, "interfaces": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress default access log spam


def connect_redis(redis_url: str | None):
    """Return a connected Redis client, or None if unavailable."""
    if not redis_url:
        return None
    try:
        import redis as redislib
        client = redislib.from_url(redis_url, socket_connect_timeout=5)
        client.ping()
        print(f"[INFO] Redis connected: {redis_url}", flush=True)
        return client
    except Exception as exc:
        print(f"[WARN] Redis unavailable ({exc}), continuing without persistence", flush=True)
        return None


def main():
    global node_id

    topology_name = os.environ.get("TOPOLOGY_NAME", "")
    node_name = os.environ.get("NODE_NAME", "")
    pod_namespace = os.environ.get("POD_NAMESPACE", "")
    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}
    api_port = int(os.environ.get("API_PORT", "9000"))
    redis_url = os.environ.get("REDIS_URL")

    if not (topology_name and node_name and pod_namespace):
        print(
            "[ERROR] TOPOLOGY_NAME, NODE_NAME, and POD_NAMESPACE are required "
            "(pass via Kubernetes downward API from clabernetes/topologyOwner and "
            "clabernetes/topologyNode labels)",
            flush=True,
        )
        sys.exit(1)

    node_id = f"{pod_namespace}/{topology_name}/{node_name}"

    poll_interval_s = poll_interval_ms / 1000.0
    redis_client = connect_redis(redis_url)

    print(f"[INFO] Starting sidecar agent for node={node_id}", flush=True)
    print(f"[INFO] HTTP server on port {api_port}, poll interval: {poll_interval_ms}ms", flush=True)
    print(f"[INFO] Excluding interfaces: {exclude}", flush=True)

    interfaces = discover_interfaces(exclude)
    print(f"[INFO] Discovered interfaces: {interfaces}", flush=True)

    if not interfaces:
        print("[ERROR] No interfaces found. Is /sys/class/net mounted?", flush=True)
        sys.exit(1)

    # Start HTTP server as daemon thread (Thread 2)
    server = HTTPServer(("0.0.0.0", api_port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[INFO] HTTP server listening on :{api_port}", flush=True)

    # Poll loop (Thread 1 — main thread)
    prev_counters = read_all_counters(interfaces)
    time.sleep(poll_interval_s)

    while True:
        interfaces = discover_interfaces(exclude)
        curr_counters = read_all_counters(interfaces)

        rates = compute_rates(prev_counters, curr_counters, poll_interval_s)

        with metrics_lock:
            metrics_store.update({m["name"]: m for m in rates})

        write_to_redis(redis_client, node_id, rates)

        prev_counters = curr_counters
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    main()
