"""
Network Monitor interface metrics collector.

Runs as a standalone Deployment. Discovers clab pods via the K8s API,
reads /proc/net/dev from each pod (universal Linux format), computes
rates, writes to Redis Streams (optional), and serves an aggregated
GET HTTP endpoint with current metrics for all discovered pods.

No sidecar injection needed — works with any pod that has /proc/net/dev
(which is every Linux container).

Environment variables:
    NAMESPACE:          Namespace to watch for clab pods (default: clab)
    POLL_INTERVAL_MS:   Polling interval in milliseconds (default: 2000)
    POD_SELECTOR:       Label selector for target pods (default: clabernetes/app=clabernetes)
    EXCLUDE_IFACES:     Comma-separated interface names to skip (default: lo)
    API_PORT:           Port for the collector HTTP server (default: 9000)
    REDIS_URL:          Redis URL, e.g. redis://:pass@host:6379/0 (optional)
    REDIS_STREAM_MAXLEN: Max entries per interface stream (default: 43200)
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from .common import compute_rates, write_to_redis
except ImportError:
    from common import compute_rates, write_to_redis

# Shared in-memory store: {node_id: [iface_dict, ...]}
metrics_store: dict = {}
metrics_lock = threading.Lock()


def get_pods(namespace: str, selector: str) -> list[dict]:
    """List target pods via kubectl."""
    try:
        result = subprocess.run(
            [
                "kubectl", "get", "pods", "-n", namespace,
                "-l", selector,
                "--field-selector=status.phase=Running",
                "-o", "jsonpath={range .items[*]}{.metadata.name} {.metadata.namespace}"
                " {.metadata.labels.clabernetes/topologyOwner}"
                " {.metadata.labels.clabernetes/topologyNode}{\"\\n\"}{end}",
            ],
            capture_output=True, text=True, timeout=10,
        )
        pods = []
        raw = result.stdout.strip()
        for line in raw.replace("\\n", "\n").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) == 4:
                pods.append({
                    "name": parts[0],
                    "namespace": parts[1],
                    "topology": parts[2],
                    "node": parts[3],
                })
        return pods
    except Exception as exc:
        print(f"[ERROR] Failed to list pods: {exc}", flush=True)
        return []


def read_proc_net_dev(pod_name: str, namespace: str) -> dict:
    """
    Read /proc/net/dev from a pod via kubectl exec.

    /proc/net/dev format:
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
        print(f"[DEBUG] Error reading from {pod_name}: {exc}", flush=True)
        return {}


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/interfaces", "/metrics"):
            with metrics_lock:
                data = [
                    {"node_id": nid, "interfaces": ifaces}
                    for nid, ifaces in metrics_store.items()
                ]
            body = json.dumps({"nodes": data}).encode()
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
        pass


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
    namespace = os.environ.get("NAMESPACE", "clab")
    poll_interval_ms = int(os.environ.get("POLL_INTERVAL_MS", "2000"))
    pod_selector = os.environ.get("POD_SELECTOR", "clabernetes/app=clabernetes")
    exclude_str = os.environ.get("EXCLUDE_IFACES", "lo")
    exclude = {s.strip() for s in exclude_str.split(",") if s.strip()}
    api_port = int(os.environ.get("API_PORT", "9000"))
    redis_url = os.environ.get("REDIS_URL")

    redis_client = connect_redis(redis_url)
    poll_interval_s = poll_interval_ms / 1000.0

    print("[INFO] Starting interface metrics collector", flush=True)
    print(f"[INFO] Namespace: {namespace}, selector: {pod_selector}", flush=True)
    print(f"[INFO] HTTP server on port {api_port}, poll interval: {poll_interval_ms}ms", flush=True)

    # Start HTTP server as daemon thread
    server = HTTPServer(("0.0.0.0", api_port), MetricsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[INFO] HTTP server listening on :{api_port}", flush=True)

    # Previous counters per pod: {node_id: {iface: counters}}
    prev_state: dict[str, dict] = {}

    while True:
        pods = get_pods(namespace, pod_selector)
        if not pods:
            print(f"[WARN] No running pods in {namespace} with selector {pod_selector}", flush=True)
            time.sleep(poll_interval_s)
            continue

        for pod in pods:
            node_id = f"{pod['namespace']}/{pod['topology']}/{pod['node']}"
            curr_counters = read_proc_net_dev(pod["name"], pod["namespace"])
            if not curr_counters:
                continue

            prev_counters = prev_state.get(node_id, {})
            rates = compute_rates(prev_counters, curr_counters, poll_interval_s, exclude)

            if rates:
                with metrics_lock:
                    metrics_store[node_id] = rates
                write_to_redis(redis_client, node_id, rates)

            prev_state[node_id] = curr_counters

        time.sleep(poll_interval_s)


if __name__ == "__main__":
    main()
