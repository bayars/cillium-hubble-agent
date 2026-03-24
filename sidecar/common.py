"""
Shared utilities for sidecar agent and standalone collector.

Contains common rate computation and API push logic.
"""

import json
import logging
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)


def compute_rates(prev: dict, curr: dict, interval_s: float, exclude: set | None = None) -> list[dict]:
    """
    Compute per-interface rates from two counter snapshots.

    Args:
        prev: Previous counter snapshot {iface: {counter: value}}
        curr: Current counter snapshot
        interval_s: Seconds between snapshots
        exclude: Interface names to skip (optional)

    Returns:
        List of per-interface metric dicts with rates and totals.
    """
    metrics = []
    for iface, counters in curr.items():
        if exclude and iface in exclude:
            continue

        entry = {
            "name": iface,
            "state": counters.get("operstate", "up"),
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
