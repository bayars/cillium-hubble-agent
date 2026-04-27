"""
Tests for the standalone collector (sidecar/collector.py).

Tests cover:
- read_proc_net_dev: parsing /proc/net/dev output
- compute_rates: rate computation with exclude filter
- get_pods: kubectl pod discovery (now returns topology/node labels)
- write_to_redis: topology-aware Redis writes
- HTTP server: /interfaces, /health endpoints
"""

import json
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar.collector import read_proc_net_dev, get_pods
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
    result = MagicMock()
    result.returncode = 1
    result.stderr = "error"

    with patch("sidecar.collector.subprocess.run", return_value=result):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


def test_read_proc_net_dev_exception():
    with patch("sidecar.collector.subprocess.run", side_effect=Exception("timeout")):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


def test_read_proc_net_dev_short_line():
    output = "Inter-|\n face |\n  eth0: 123\n"
    result = MagicMock()
    result.returncode = 0
    result.stdout = output

    with patch("sidecar.collector.subprocess.run", return_value=result):
        interfaces = read_proc_net_dev("test-pod", "clab")
    assert interfaces == {}


# ---------------------------------------------------------------------------
# compute_rates (shared logic)
# ---------------------------------------------------------------------------


def test_compute_rates_basic():
    prev = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 10, "tx_packets": 5,
                     "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}}
    curr = {"eth0": {"rx_bytes": 3000, "tx_bytes": 1500, "rx_packets": 30, "tx_packets": 15,
                     "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
            "lo":   {"rx_bytes": 200, "tx_bytes": 200, "rx_packets": 2, "tx_packets": 2,
                     "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}}

    metrics = compute_rates(prev, curr, interval_s=2.0, exclude={"lo"})
    assert len(metrics) == 1
    m = metrics[0]
    assert m["name"] == "eth0"
    assert m["rx_bps"] == 1000.0
    assert m["tx_bps"] == 500.0


def test_compute_rates_counter_wrap():
    prev = {"eth0": {"rx_bytes": 10000, "tx_bytes": 5000, "rx_packets": 100, "tx_packets": 50,
                     "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}}
    curr = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 100, "tx_packets": 50,
                     "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}}
    metrics = compute_rates(prev, curr, interval_s=1.0, exclude=set())
    assert metrics[0]["rx_bps"] == 0.0


# ---------------------------------------------------------------------------
# get_pods
# ---------------------------------------------------------------------------


def test_get_pods_parses_output():
    result = MagicMock()
    result.stdout = "pod1 clab my-topo R1\\npod2 clab my-topo R2"

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("clab", "clabernetes/app=clabernetes")

    assert len(pods) == 2
    assert pods[0] == {"name": "pod1", "namespace": "clab", "topology": "my-topo", "node": "R1"}
    assert pods[1] == {"name": "pod2", "namespace": "clab", "topology": "my-topo", "node": "R2"}


def test_get_pods_empty_output():
    result = MagicMock()
    result.stdout = ""

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("clab", "clabernetes/app=clabernetes")
    assert pods == []


def test_get_pods_exception():
    with patch("sidecar.collector.subprocess.run", side_effect=Exception("fail")):
        pods = get_pods("clab", "clabernetes/app=clabernetes")
    assert pods == []


def test_get_pods_no_labels():
    """Pods without topology labels still return with empty topology/node fields."""
    result = MagicMock()
    result.stdout = "pod1 clab"  # no labels

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("clab", "clabernetes/app=clabernetes")

    assert len(pods) == 1
    assert pods[0]["name"] == "pod1"
    assert pods[0]["topology"] == ""
    assert pods[0]["node"] == ""


# ---------------------------------------------------------------------------
# write_to_redis (shared logic — same as agent tests)
# ---------------------------------------------------------------------------


def test_write_to_redis_creates_topology_stream(redis_client):
    """write_to_redis uses topology-aware key schema."""
    interfaces = [{"name": "e1-1", "rx_bps": 2000.0, "tx_bps": 1000.0, "state": "up"}]
    write_to_redis(redis_client, "clab", "my-topo", "R1", interfaces)

    stream_key = "nm:topo:clab:my-topo:R1:e1-1"
    entries = redis_client.xrange(stream_key)
    assert len(entries) == 1
    assert entries[0][1]["rx_bps"] == "2000.0"


def test_write_to_redis_noop_without_client():
    write_to_redis(None, "clab", "my-topo", "R1", [{"name": "e1-1"}])


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def test_collector_http_health():
    """GET /health returns 200."""
    import http.client
    from http.server import HTTPServer
    from sidecar.collector import make_handler

    server = HTTPServer(("127.0.0.1", 0), make_handler(store={}, lock=threading.Lock()))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    server.shutdown()

    assert resp.status == 200
    assert json.loads(resp.read()) == {"status": "ok"}


def test_collector_http_interfaces():
    """GET /interfaces returns nodes list."""
    import http.client
    from http.server import HTTPServer
    from sidecar.collector import make_handler

    store = {
        "clab/my-topo/R1": {
            "node_id": "clab/my-topo/R1",
            "interfaces": [{"name": "e1-1", "rx_bps": 100.0}],
        }
    }
    lock = threading.Lock()

    server = HTTPServer(("127.0.0.1", 0), make_handler(store=store, lock=lock))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", "/interfaces")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    server.shutdown()

    assert resp.status == 200
    assert "nodes" in body
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["node_id"] == "clab/my-topo/R1"
