"""
Shared utilities for sidecar agent and standalone collector.

Contains common rate computation and Redis write logic.
"""

import os

STREAM_MAXLEN = int(os.environ.get("REDIS_STREAM_MAXLEN", "43200"))

# Key schema (colon-separated, no slashes inside segments):
#   nm:topo:{namespace}:{topology}:{node}:{iface}  → Stream
#   nm:topo:{namespace}:{topology}:{node}:ifaces   → Set  (interface index)
#   nm:topo:{namespace}:{topology}:nodes           → Set  (node index)
#   nm:topologies                                  → Set  (topology index, values: "ns/topo")
KEY_PREFIX = "nm"


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


def write_to_redis(redis_client, node_id: str, interfaces: list[dict]) -> None:
    """Pipeline XADD for all interfaces into Redis Streams. Silent on failure.

    node_id must be in the form "{namespace}/{topology}/{node}", e.g.
    "default/srl-probe-test/srl1". This maps to the key schema:
        nm:topo:{namespace}:{topology}:{node}:{iface}  → Stream
        nm:topo:{namespace}:{topology}:{node}:ifaces   → Set
        nm:topo:{namespace}:{topology}:nodes           → Set
        nm:topologies                                  → Set
    """
    if redis_client is None:
        return
    try:
        parts = node_id.split("/", 2)
        if len(parts) != 3:
            print(f"[WARN] node_id '{node_id}' is not in ns/topology/node format, skipping Redis write", flush=True)
            return
        namespace, topology, node = parts
        topo_key = f"{KEY_PREFIX}:topo:{namespace}:{topology}"

        pipe = redis_client.pipeline(transaction=False)
        for iface in interfaces:
            stream_key = f"{topo_key}:{node}:{iface['name']}"
            fields = {k: str(v) for k, v in iface.items() if k != "name"}
            pipe.xadd(stream_key, fields, maxlen=STREAM_MAXLEN, approximate=True)
            pipe.sadd(f"{topo_key}:{node}:ifaces", iface["name"])
        pipe.sadd(f"{topo_key}:nodes", node)
        pipe.sadd(f"{KEY_PREFIX}:topologies", f"{namespace}/{topology}")
        pipe.execute()
    except Exception as exc:
        print(f"[WARN] Redis write failed: {exc}", flush=True)
