"""
Network Monitor interface metrics collector.

Runs as a standalone Deployment. Discovers clab pods via the K8s API,
reads /proc/net/dev from each pod (universal Linux format), computes
rates, and pushes per-interface metrics to the network-monitor API.

No sidecar injection needed — works with any pod that has /proc/net/dev
(which is every Linux container).

Environment variables:
    API_URL:          Network monitor API base URL (required)
    NAMESPACE:        Namespace to watch for clab pods (default: clab)
    POLL_INTERVAL_MS: Polling interval in milliseconds (default: 2000)
    POD_SELECTOR:     Label selector for target pods (default: clabernetes/app=clabernetes)
    EXCLUDE_IFACES:   Comma-separated interface names to skip (default: lo)
    LOG_LEVEL:        Logging level (default: INFO)
"""

import json
import logging
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] collector: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("collector")


def get_pods(namespace: str, selector: str) -> list[dict]:
    """List target pods via kubectl."""
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", namespace,
                "-l", selector,
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={range .items[*]}{.metadata.name} {.metadata.namespace}{\"\\n\"}{end}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        pods = []
        # jsonpath \n comes through as literal backslash-n
        raw = result.stdout.strip()
        for line in raw.replace("\\n", "\n").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 2:
                pods.append({"name": parts[0], "namespace": parts[1]})
        return pods
    except Exception as e:
        logger.error(f"Failed to list pods: {e}")
        return []


def read_proc_net_dev(pod_name: str, namespace: str) -> dict:
    """
    Read /proc/net/dev from a pod via kubectl exec.

    /proc/net/dev has a universal format across all Linux containers:
    Inter-|   Receive                                                |  Transmit
     face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
      eth0: 12345   100      0    0    0     0          0         0    6789    50       0    0    0     0       0          0
    """
    try:
        result = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod_name, "--", "cat", "/proc/net/dev"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            logger.debug(f"Failed to read /proc/net/dev from {pod_name}: {result.stderr.strip()}")
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
    except Exception as e:
        logger.debug(f"Error reading from {pod_name}: {e}")
        return {}


def get_operstate(pod_name: str, namespace: str, iface: str) -> str:
    """Read interface operstate from a pod."""
    try:
        result = subprocess.run(
            ["kubectl", "exec", "-n", namespace, pod_name, "--",
             "cat", f"/sys/class/net/{iface}/operstate"],
            capture_output=True, text=True, timeout=5,
        )
        state = result.stdout.strip()
        return "up" if state == "up" else "down"
    except Exception:
        return "unknown"


def compute_rates(prev: dict, curr: dict, interval_s: float, exclude: set) -> list[dict]:
    """Compute per-interface rates."""
    metrics = []
    for iface, counters in curr.items():
        if iface in exclude:
            continue

        entry = {
            "name": iface,
            "state": "up",  # If it appears in /proc/net/dev, it exists
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
                logger.debug(f"Pushed {len(interfaces)} interfaces for {node_id}")
            else:
                logger.warning(f"API returned {resp.status} for {node_id}")
    except URLError as e:
        logger.warning(f"Failed to push metrics for {node_id}: {e}")


def main():
    api_url = os.environ.get("API_URL")
    namespace = os.environ.get("NAMESPACE", "clab")
    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    pod_selector = os.environ.get("POD_SELECTOR", "clabernetes/app=clabernetes")
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}

    if not api_url:
        logger.error("API_URL environment variable is required")
        sys.exit(1)

    poll_interval_s = poll_interval_ms / 1000.0

    logger.info(f"Starting interface metrics collector")
    logger.info(f"API: {api_url}, namespace: {namespace}, selector: {pod_selector}")
    logger.info(f"Poll interval: {poll_interval_ms}ms, excluding: {exclude}")

    # Previous counters per pod: {node_id: {iface: counters}}
    prev_state: dict[str, dict] = {}

    while True:
        pods = get_pods(namespace, pod_selector)
        if not pods:
            logger.warning(f"No running pods found in {namespace} with selector {pod_selector}")
            time.sleep(poll_interval_s)
            continue

        logger.info(f"Found {len(pods)} pods")
        for pod in pods:
            node_id = f"{pod['namespace']}/{pod['name']}"

            curr_counters = read_proc_net_dev(pod["name"], pod["namespace"])
            if not curr_counters:
                logger.debug(f"No counters for {node_id}")
                continue

            prev_counters = prev_state.get(node_id, {})
            metrics = compute_rates(prev_counters, curr_counters, poll_interval_s, exclude)

            if metrics:
                push_metrics(api_url, node_id, metrics, poll_interval_ms)
                logger.info(f"Pushed {len(metrics)} interfaces for {node_id}")

            prev_state[node_id] = curr_counters

        time.sleep(poll_interval_s)


if __name__ == "__main__":
    main()
