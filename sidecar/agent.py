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
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] sidecar: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sidecar")

SYSFS_NET = Path("/sys/class/net")

# Counters we read from sysfs
BYTE_COUNTERS = ("rx_bytes", "tx_bytes")
PACKET_COUNTERS = ("rx_packets", "tx_packets")
ERROR_COUNTERS = ("rx_errors", "tx_errors")
DROP_COUNTERS = ("rx_dropped", "tx_dropped")


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


def compute_rates(prev: dict, curr: dict, interval_s: float) -> list[dict]:
    """Compute per-interface rates from two counter snapshots."""
    metrics = []
    for iface, counters in curr.items():
        entry = {
            "name": iface,
            "state": counters["operstate"],
            "rx_bytes_total": counters["rx_bytes"],
            "tx_bytes_total": counters["tx_bytes"],
            "rx_packets_total": counters["rx_packets"],
            "tx_packets_total": counters["tx_packets"],
            "rx_errors": counters["rx_errors"],
            "tx_errors": counters["tx_errors"],
            "rx_dropped": counters["rx_dropped"],
            "tx_dropped": counters["tx_dropped"],
            "rx_bps": 0.0,
            "tx_bps": 0.0,
            "rx_pps": 0.0,
            "tx_pps": 0.0,
        }

        if iface in prev and interval_s > 0:
            p = prev[iface]
            entry["rx_bps"] = max(0, (counters["rx_bytes"] - p["rx_bytes"])) / interval_s
            entry["tx_bps"] = max(0, (counters["tx_bytes"] - p["tx_bytes"])) / interval_s
            entry["rx_pps"] = max(0, (counters["rx_packets"] - p["rx_packets"])) / interval_s
            entry["tx_pps"] = max(0, (counters["tx_packets"] - p["tx_packets"])) / interval_s

        metrics.append(entry)
    return metrics


def push_metrics(api_url: str, node_id: str, interfaces: list[dict], poll_interval_ms: int):
    """Push interface metrics to the network-monitor API."""
    url = f"{api_url}/api/interfaces"
    payload = json.dumps({
        "node_id": node_id,
        "interfaces": interfaces,
        "poll_interval_ms": poll_interval_ms,
        "data_source": "sysfs",
    }).encode()

    req = Request(url, data=payload, method="PUT")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                logger.debug(f"Pushed {len(interfaces)} interfaces to {url}")
            else:
                logger.warning(f"API returned {resp.status}")
    except URLError as e:
        logger.warning(f"Failed to push metrics: {e}")


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
