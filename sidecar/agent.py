"""
Network Monitor sidecar agent — unified VXLAN bandwidth monitoring.

Runs as an extraContainer in Clabernetes launcher pods. Reads per-interface
counters from /sys/class/net/*/statistics/ and maps VXLAN interface names
(vx-{node}-{iface}) to logical topology interface names via the Clabernetes
Connectivity CR.

Works for all NOS types (SR Linux, SR OS, SR SIM, vr-sros, FRR, VyOS) because
Clabernetes always routes inter-node traffic through VXLAN tunnels in the
launcher pod's network namespace, and Linux VXLAN interface counters reflect
inner (payload) bytes — not encapsulation overhead.

Two threads:
  Thread 1 (main):   poll loop — reads counters, maps names, updates store
  Thread 2 (daemon): HTTP server — serves current metrics_store via GET

Background daemon: refreshes Connectivity CR every CONNECTIVITY_REFRESH_S.

Environment variables:
  POD_NAMESPACE:          K8s namespace (required, from downward API)
  TOPOLOGY_NAME:          Clabernetes topology name (required, from downward API)
  NODE_NAME:              Node name within topology (required, from downward API)
  POLL_INTERVAL_MS:       Poll interval in ms (default: 2000)
  EXCLUDE_IFACES:         Comma-separated interfaces to skip (default: lo,eth0)
  API_PORT:               HTTP server port (default: 9000)
  REDIS_URL:              Redis connection URL (optional)
  REDIS_STREAM_MAXLEN:    Max stream entries per interface (default: 43200)
  CONNECTIVITY_REFRESH_S: How often to re-read Connectivity CR (default: 60)
  LOG_LEVEL:              Logging level (default: INFO)
"""

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from .common import compute_rates, write_to_redis
    from .connectivity import ConnectivityResolver, refresh_loop
except ImportError:
    from common import compute_rates, write_to_redis
    from connectivity import ConnectivityResolver, refresh_loop

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] sidecar: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sidecar")

SYSFS_NET = Path("/sys/class/net")

# ---------------------------------------------------------------------------
# Shared metrics store (updated by poll loop, read by HTTP server)
# ---------------------------------------------------------------------------

metrics_store: dict = {}   # {iface_name: dict}
metrics_lock = threading.Lock()


# ---------------------------------------------------------------------------
# sysfs counter helpers
# ---------------------------------------------------------------------------


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
    """List network interfaces that have a statistics/ directory."""
    if not SYSFS_NET.exists():
        logger.error("%s does not exist — is /sys mounted?", SYSFS_NET)
        return []
    return [
        d.name
        for d in SYSFS_NET.iterdir()
        if (d.is_symlink() or d.is_dir())
        and d.name not in exclude
        and (d / "statistics").exists()
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


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def make_handler(node_id: str, store: dict | None = None, lock: threading.Lock | None = None):
    """Return an HTTPRequestHandler class closed over node_id, store, and lock."""
    _store = store if store is not None else metrics_store
    _lock = lock if lock is not None else metrics_lock

    class MetricsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/interfaces", "/metrics"):
                with _lock:
                    data = list(_store.values())
                body = json.dumps({"node_id": node_id, "interfaces": data}).encode()
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
            pass  # suppress default access log noise

    return MetricsHandler


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
    namespace = os.environ.get("POD_NAMESPACE")
    topology = os.environ.get("TOPOLOGY_NAME")
    node_name = os.environ.get("NODE_NAME")

    if not namespace or not topology or not node_name:
        logger.error(
            "POD_NAMESPACE, TOPOLOGY_NAME, and NODE_NAME are required "
            "(set via Kubernetes downward API)"
        )
        sys.exit(1)

    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo,eth0")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}
    api_port = int(os.environ.get("API_PORT", "9000"))
    redis_url = os.environ.get("REDIS_URL")
    connectivity_refresh_s = int(os.environ.get("CONNECTIVITY_REFRESH_S", "60"))

    poll_interval_s = poll_interval_ms / 1000.0
    node_id = f"{namespace}/{topology}/{node_name}"

    logger.info("Starting sidecar agent")
    logger.info("  node:      %s", node_id)
    logger.info("  poll:      %dms", poll_interval_ms)
    logger.info("  http port: %d", api_port)
    logger.info("  exclude:   %s", exclude)
    logger.info("  redis:     %s", "enabled" if redis_url else "disabled")

    # Connectivity CR resolver (VXLAN → logical name mapping)
    connectivity = ConnectivityResolver(namespace, topology, node_name)
    try:
        connectivity.refresh()
        logger.info("Connectivity CR loaded: %d VXLAN links", connectivity.link_count)
    except Exception as exc:
        logger.warning("Initial Connectivity CR load failed: %s — using raw VXLAN names", exc)

    # Redis client (optional)
    redis_client = connect_redis(redis_url)

    # Background: Connectivity CR refresh
    stop_event = threading.Event()
    refresh_thread = threading.Thread(
        target=refresh_loop,
        args=(connectivity, connectivity_refresh_s, stop_event),
        daemon=True,
    )
    refresh_thread.start()

    # HTTP server thread
    server = HTTPServer(("0.0.0.0", api_port), make_handler(node_id))
    http_thread = threading.Thread(target=server.serve_forever, daemon=True)
    http_thread.start()
    logger.info("HTTP server listening on port %d", api_port)

    # Initial counter snapshot
    interfaces = discover_interfaces(exclude)
    if not interfaces:
        logger.warning("No interfaces found on first poll — will retry each interval")
    prev_counters = read_all_counters(interfaces)
    time.sleep(poll_interval_s)

    # Main poll loop
    try:
        while True:
            interfaces = discover_interfaces(exclude)
            curr_counters = read_all_counters(interfaces)

            rates = compute_rates(prev_counters, curr_counters, poll_interval_s)

            # Map VXLAN interface names to logical topology names
            mapped = []
            for iface_dict in rates:
                raw_name = iface_dict["name"]
                if raw_name.startswith("vx-"):
                    link = connectivity.resolve_vxlan(raw_name)
                    if link:
                        iface_dict = dict(iface_dict)
                        iface_dict["name"] = link.logical_iface
                        iface_dict["source"] = "vxlan"
                    else:
                        iface_dict = dict(iface_dict)
                        iface_dict["source"] = "vxlan_raw"
                else:
                    iface_dict = dict(iface_dict)
                    iface_dict["source"] = "kernel"
                mapped.append(iface_dict)

            with metrics_lock:
                metrics_store.clear()
                metrics_store.update({m["name"]: m for m in mapped})

            write_to_redis(redis_client, namespace, topology, node_name, mapped)

            prev_counters = curr_counters
            time.sleep(poll_interval_s)

    except KeyboardInterrupt:
        logger.info("Shutting down")
        stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
