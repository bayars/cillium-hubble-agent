"""
Tests for the standalone collector (sidecar/collector.py).

Tests cover:
- read_proc_net_dev: parsing /proc/net/dev output
- compute_rates: rate computation with exclude filter
- get_pods: kubectl pod discovery
- write_to_redis: Redis Streams write with fakeredis
- HTTP server: GET /interfaces and /health responses
- main: entry point validation
"""

import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from sidecar.collector import (
    read_proc_net_dev,
    get_pods,
)
from sidecar.common import compute_rates, write_to_redis


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROC_NET_DEV_OUTPUT = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:  123456     100    0    0    0     0          0         0   123456     100    0    0    0     0       0          0
  eth0: 9876543    5000    2    1    0     0          0         0  4567890    2500    0    0    0     0       0          0
  eth1: 1111111    1000    0    0    0     0          0         0  2222222    2000    1    3    0     0       0          0
"""


# ---------------------------------------------------------------------------
# read_proc_net_dev
# ---------------------------------------------------------------------------


def test_read_proc_net_dev_parses_output():
    """Parses /proc/net/dev output correctly."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = PROC_NET_DEV_OUTPUT

    with patch("sidecar.collector.subprocess.run", return_value=result):
        interfaces = read_proc_net_dev("test-pod", "clab")

    assert "lo" in interfaces
    assert "eth0" in interfaces
    assert "eth1" in interfaces

    assert interfaces["eth0"]["rx_bytes"] == 9876543
    assert interfaces["eth0"]["tx_bytes"] == 4567890
    assert interfaces["eth0"]["rx_packets"] == 5000
    assert interfaces["eth0"]["tx_packets"] == 2500
    assert interfaces["eth0"]["rx_errors"] == 2
    assert interfaces["eth0"]["rx_dropped"] == 1
    assert interfaces["eth1"]["tx_errors"] == 1
    assert interfaces["eth1"]["tx_dropped"] == 3


def test_read_proc_net_dev_command_failure():
    """Returns empty dict when kubectl exec fails."""
    result = MagicMock()
    result.returncode = 1
    result.stderr = "error"

    with patch("sidecar.collector.subprocess.run", return_value=result):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


def test_read_proc_net_dev_exception():
    """Returns empty dict on subprocess exception."""
    with patch("sidecar.collector.subprocess.run", side_effect=Exception("timeout")):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


def test_read_proc_net_dev_short_line():
    """Skips lines with fewer than 16 fields."""
    output = """\
Inter-|   Receive
 face |bytes
  eth0: 123
"""
    result = MagicMock()
    result.returncode = 0
    result.stdout = output

    with patch("sidecar.collector.subprocess.run", return_value=result):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


# ---------------------------------------------------------------------------
# compute_rates
# ---------------------------------------------------------------------------


def test_compute_rates_basic():
    """Computes rates from two snapshots with exclusion."""
    prev = {
        "eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 10, "tx_packets": 5,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
        "lo": {"rx_bytes": 100, "tx_bytes": 100, "rx_packets": 1, "tx_packets": 1,
               "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }
    curr = {
        "eth0": {"rx_bytes": 3000, "tx_bytes": 1500, "rx_packets": 30, "tx_packets": 15,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
        "lo": {"rx_bytes": 200, "tx_bytes": 200, "rx_packets": 2, "tx_packets": 2,
               "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }

    metrics = compute_rates(prev, curr, interval_s=2.0, exclude={"lo"})
    assert len(metrics) == 1
    m = metrics[0]
    assert m["name"] == "eth0"
    assert m["rx_bps"] == 1000.0
    assert m["tx_bps"] == 500.0
    assert m["rx_pps"] == 10.0
    assert m["tx_pps"] == 5.0


def test_compute_rates_no_previous():
    """First snapshot returns zero rates."""
    curr = {
        "eth0": {"rx_bytes": 5000, "tx_bytes": 3000, "rx_packets": 50, "tx_packets": 30,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }

    metrics = compute_rates({}, curr, interval_s=2.0, exclude=set())
    assert len(metrics) == 1
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["rx_bytes_total"] == 5000


def test_compute_rates_counter_wrap():
    """Counter wrap clamps to zero."""
    prev = {
        "eth0": {"rx_bytes": 10000, "tx_bytes": 5000, "rx_packets": 100, "tx_packets": 50,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }
    curr = {
        "eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 100, "tx_packets": 50,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }

    metrics = compute_rates(prev, curr, interval_s=1.0, exclude=set())
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["tx_bps"] == 0.0


def test_compute_rates_state_is_up():
    """All interfaces from /proc/net/dev get state='up' (no operstate file)."""
    curr = {
        "eth0": {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    }

    metrics = compute_rates({}, curr, interval_s=1.0, exclude=set())
    assert metrics[0]["state"] == "up"


# ---------------------------------------------------------------------------
# get_pods
# ---------------------------------------------------------------------------


def test_get_pods_parses_output():
    """Parses kubectl output into pod list with topology and node labels."""
    result = MagicMock()
    result.stdout = "srl-probe-test-srl1-abc default srl-probe-test srl1\\ndc1-spine1-xyz default dc1 spine1"

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("default", "clabernetes/app=clabernetes")

    assert len(pods) == 2
    assert pods[0] == {"name": "srl-probe-test-srl1-abc", "namespace": "default",
                       "topology": "srl-probe-test", "node": "srl1"}
    assert pods[1] == {"name": "dc1-spine1-xyz", "namespace": "default",
                       "topology": "dc1", "node": "spine1"}


def test_get_pods_empty_output():
    """Returns empty list when no pods found."""
    result = MagicMock()
    result.stdout = ""

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("clab", "clabernetes/app=clabernetes")
    assert pods == []


def test_get_pods_exception():
    """Returns empty list on exception."""
    with patch("sidecar.collector.subprocess.run", side_effect=Exception("fail")):
        pods = get_pods("clab", "clabernetes/app=clabernetes")
    assert pods == []


# ---------------------------------------------------------------------------
# write_to_redis (via shared common)
# ---------------------------------------------------------------------------


def test_write_to_redis_collector(redis_client):
    """write_to_redis creates stream under nm:topo:{ns}:{topo}:{node}:{iface}."""
    interfaces = [
        {"name": "eth0", "rx_bps": 500.0, "tx_bps": 250.0,
         "rx_bytes_total": 5000, "tx_bytes_total": 2500,
         "rx_packets_total": 50, "tx_packets_total": 25,
         "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
         "state": "up", "rx_pps": 5.0, "tx_pps": 2.5},
    ]

    write_to_redis(redis_client, "default/dc1/leaf1", interfaces)

    entries = redis_client.xrange("nm:topo:default:dc1:leaf1:eth0")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["rx_bps"] == "500.0"
    assert "name" not in fields


def test_write_to_redis_collector_none_client():
    """write_to_redis is a no-op with None client."""
    write_to_redis(None, "clab/leaf1", [{"name": "eth0"}])


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _start_collector_server(nodes_data: dict) -> int:
    """Start the collector HTTP server and return the port."""
    import sidecar.collector as collector_mod
    from http.server import HTTPServer
    from sidecar.collector import MetricsHandler

    with collector_mod.metrics_lock:
        collector_mod.metrics_store.clear()
        collector_mod.metrics_store.update(nodes_data)

    server = HTTPServer(("127.0.0.1", 0), MetricsHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def test_collector_http_interfaces_endpoint():
    """GET /interfaces returns all nodes with their interfaces."""
    data = {
        "clab/r1": [{"name": "eth0", "rx_bps": 100.0}],
        "clab/r2": [{"name": "eth1", "rx_bps": 200.0}],
    }
    port = _start_collector_server(data)

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/interfaces")
    body = json.loads(resp.read())

    assert "nodes" in body
    node_ids = {n["node_id"] for n in body["nodes"]}
    assert "clab/r1" in node_ids
    assert "clab/r2" in node_ids


def test_collector_http_health_endpoint():
    """GET /health returns {"status": "ok"}."""
    port = _start_collector_server({})

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
    data = json.loads(resp.read())
    assert data["status"] == "ok"


def test_collector_http_404():
    """Unknown paths return 404."""
    port = _start_collector_server({})

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown")
        assert False, "Expected HTTPError"
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
