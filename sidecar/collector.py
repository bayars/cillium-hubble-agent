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

import logging
import os
import subprocess
import sys
import time

try:
    from .common import compute_rates, push_metrics
except ImportError:
    from common import compute_rates, push_metrics

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

    logger.info("Starting interface metrics collector")
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
