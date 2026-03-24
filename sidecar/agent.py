"""
Network Monitor sidecar agent.

Reads per-interface rx/tx counters from /sys/class/net/*/statistics/
and pushes rates to the network-monitor API. Captures ALL traffic
(ping, ssh, scp, routing protocols, etc.) on every interface.

Runs as a sidecar container (via Clabernetes extraContainers) sharing
the pod's network namespace, so it sees all interfaces including
linecards, CPM, and mgmt interfaces.

Node ID auto-detection (in priority order):
    1. NODE_ID env var (explicit override)
    2. POD_NAME + POD_NAMESPACE env vars (Kubernetes downward API)

Environment variables:
    API_URL:          Network monitor API base URL (required)
    NODE_ID:          Node identifier (optional, auto-detected from pod metadata)
    POD_NAME:         Pod name from downward API (auto-detection fallback)
    POD_NAMESPACE:    Pod namespace from downward API (auto-detection fallback)
    POLL_INTERVAL_MS: Polling interval in milliseconds (default: 2000)
    EXCLUDE_IFACES:   Comma-separated interface names to skip (default: lo)
    LOG_LEVEL:        Logging level (default: INFO)
"""

import logging
import os
import sys
import time
from pathlib import Path

try:
    from .common import compute_rates, push_metrics
except ImportError:
    from common import compute_rates, push_metrics

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] sidecar: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sidecar")

SYSFS_NET = Path("/sys/class/net")


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
        logger.error(f"{SYSFS_NET} does not exist")
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


def main():
    api_url = os.environ.get("API_URL")
    node_id = os.environ.get("NODE_ID")
    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}

    if not api_url:
        logger.error("API_URL environment variable is required")
        sys.exit(1)

    # Auto-detect node_id from pod name if not explicitly set
    if not node_id:
        pod_name = os.environ.get("POD_NAME")
        pod_namespace = os.environ.get("POD_NAMESPACE")
        if pod_name and pod_namespace:
            node_id = f"{pod_namespace}/{pod_name}"
        else:
            logger.error("NODE_ID or (POD_NAME + POD_NAMESPACE) environment variables required")
            sys.exit(1)

    poll_interval_s = poll_interval_ms / 1000.0

    logger.info(f"Starting sidecar agent for node={node_id}")
    logger.info(f"API: {api_url}, poll interval: {poll_interval_ms}ms")
    logger.info(f"Excluding interfaces: {exclude}")

    # Initial discovery and snapshot
    interfaces = discover_interfaces(exclude)
    logger.info(f"Discovered interfaces: {interfaces}")

    if not interfaces:
        logger.error("No interfaces found. Is /sys/class/net mounted?")
        sys.exit(1)

    prev_counters = read_all_counters(interfaces)
    time.sleep(poll_interval_s)

    while True:
        # Re-discover interfaces periodically (handles hotplug)
        interfaces = discover_interfaces(exclude)
        curr_counters = read_all_counters(interfaces)

        metrics = compute_rates(prev_counters, curr_counters, poll_interval_s)
        push_metrics(api_url, node_id, metrics, poll_interval_ms)

        prev_counters = curr_counters
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    main()
