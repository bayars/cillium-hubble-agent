"""
Tests for sidecar/agent.py and sidecar/common.py.

Tests cover:
- discover_interfaces: listing network interfaces from sysfs
- read_counter: reading individual sysfs counter files
- get_operstate: reading interface operational state
- read_all_counters: bulk counter reads
- compute_rates: rate computation from two counter snapshots
- write_to_redis: topology-aware Redis Stream writes
- HTTP server: /interfaces, /health endpoints
- VXLAN name mapping via ConnectivityResolver
- main(): entry point validation
"""

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar.agent import (
    discover_interfaces,
    read_counter,
    get_operstate,
    read_all_counters,
    make_handler,
)
from sidecar.common import compute_rates, write_to_redis


# ---------------------------------------------------------------------------
# discover_interfaces
# ---------------------------------------------------------------------------


def test_discover_interfaces(tmp_path):
    for iface in ("eth0", "eth1", "lo"):
        d = tmp_path / iface
        d.mkdir()
        (d / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude={"lo"})
    assert sorted(result) == ["eth0", "eth1"]


def test_discover_interfaces_excludes_multiple(tmp_path):
    for iface in ("eth0", "eth1", "docker0", "lo"):
        d = tmp_path / iface
        d.mkdir()
        (d / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude={"lo", "docker0"})
    assert sorted(result) == ["eth0", "eth1"]


def test_discover_interfaces_no_statistics_dir(tmp_path):
    (tmp_path / "eth0").mkdir()
    iface_with = tmp_path / "eth1"
    iface_with.mkdir()
    (iface_with / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude=set())
    assert result == ["eth1"]


def test_discover_interfaces_sysfs_missing(tmp_path):
    missing = tmp_path / "nonexistent"
    with patch("sidecar.agent.SYSFS_NET", missing):
        result = discover_interfaces(exclude=set())
    assert result == []


# ---------------------------------------------------------------------------
# read_counter
# ---------------------------------------------------------------------------


def test_read_counter(tmp_path):
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_bytes").write_text("123456\n")
    assert read_counter(tmp_path, "rx_bytes") == 123456


def test_read_counter_missing_file(tmp_path):
    assert read_counter(tmp_path, "rx_bytes") == 0


def test_read_counter_invalid_content(tmp_path):
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_bytes").write_text("not_a_number\n")
    assert read_counter(tmp_path, "rx_bytes") == 0


# ---------------------------------------------------------------------------
# get_operstate
# ---------------------------------------------------------------------------


def test_get_operstate_up(tmp_path):
    (tmp_path / "operstate").write_text("up\n")
    assert get_operstate(tmp_path) == "up"


def test_get_operstate_down(tmp_path):
    (tmp_path / "operstate").write_text("down\n")
    assert get_operstate(tmp_path) == "down"


def test_get_operstate_unknown_value(tmp_path):
    (tmp_path / "operstate").write_text("dormant\n")
    assert get_operstate(tmp_path) == "down"


def test_get_operstate_missing_file(tmp_path):
    assert get_operstate(tmp_path) == "unknown"


# ---------------------------------------------------------------------------
# read_all_counters
# ---------------------------------------------------------------------------


def _make_sysfs_iface(base: Path, name: str, counters: dict, operstate: str = "up"):
    iface_dir = base / name
    iface_dir.mkdir(exist_ok=True)
    stats = iface_dir / "statistics"
    stats.mkdir(exist_ok=True)
    for counter, value in counters.items():
        (stats / counter).write_text(str(value) + "\n")
    (iface_dir / "operstate").write_text(operstate + "\n")


def test_read_all_counters(tmp_path):
    _make_sysfs_iface(tmp_path, "eth0", {
        "rx_bytes": 1000, "tx_bytes": 500,
        "rx_packets": 10, "tx_packets": 5,
        "rx_errors": 1, "tx_errors": 0,
        "rx_dropped": 2, "tx_dropped": 0,
    })

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = read_all_counters(["eth0"])

    assert "eth0" in result
    assert result["eth0"]["rx_bytes"] == 1000
    assert result["eth0"]["tx_bytes"] == 500
    assert result["eth0"]["operstate"] == "up"


# ---------------------------------------------------------------------------
# compute_rates
# ---------------------------------------------------------------------------


def test_compute_rates_with_previous():
    prev = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 100,
                     "tx_packets": 50, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}
    curr = {"eth0": {"rx_bytes": 3000, "tx_bytes": 1500, "rx_packets": 200,
                     "tx_packets": 100, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}

    metrics = compute_rates(prev, curr, interval_s=2.0)
    m = metrics[0]
    assert m["name"] == "eth0"
    assert m["rx_bps"] == 1000.0
    assert m["tx_bps"] == 500.0
    assert m["rx_pps"] == 50.0
    assert m["tx_pps"] == 25.0


def test_compute_rates_no_previous():
    curr = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 10,
                     "tx_packets": 5, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}
    metrics = compute_rates({}, curr, interval_s=2.0)
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["tx_bps"] == 0.0


def test_compute_rates_counter_wrap():
    prev = {"eth0": {"rx_bytes": 5000, "tx_bytes": 3000, "rx_packets": 100,
                     "tx_packets": 50, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}
    curr = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 100,
                     "tx_packets": 50, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}
    metrics = compute_rates(prev, curr, interval_s=1.0)
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["tx_bps"] == 0.0


def test_compute_rates_zero_interval():
    prev = {"eth0": {"rx_bytes": 1000, "tx_bytes": 500, "rx_packets": 10,
                     "tx_packets": 5, "rx_errors": 0, "tx_errors": 0,
                     "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"}}
    curr = dict(prev)
    metrics = compute_rates(prev, curr, interval_s=0)
    assert metrics[0]["rx_bps"] == 0.0


def test_compute_rates_multiple_interfaces():
    prev = {
        "eth0": {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"},
        "eth1": {"rx_bytes": 0, "tx_bytes": 0, "rx_packets": 0, "tx_packets": 0,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"},
    }
    curr = {
        "eth0": {"rx_bytes": 2000, "tx_bytes": 1000, "rx_packets": 20, "tx_packets": 10,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"},
        "eth1": {"rx_bytes": 4000, "tx_bytes": 2000, "rx_packets": 40, "tx_packets": 20,
                 "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0, "operstate": "up"},
    }
    metrics = compute_rates(prev, curr, interval_s=1.0)
    by_name = {m["name"]: m for m in metrics}
    assert by_name["eth0"]["rx_bps"] == 2000.0
    assert by_name["eth1"]["rx_bps"] == 4000.0


def test_compute_rates_includes_totals():
    curr = {"eth0": {"rx_bytes": 99999, "tx_bytes": 88888, "rx_packets": 1000,
                     "tx_packets": 900, "rx_errors": 5, "tx_errors": 3,
                     "rx_dropped": 2, "tx_dropped": 1, "operstate": "up"}}
    metrics = compute_rates({}, curr, interval_s=1.0)
    m = metrics[0]
    assert m["rx_bytes_total"] == 99999
    assert m["rx_errors"] == 5
    assert m["rx_dropped"] == 2


def test_compute_rates_new_interface():
    metrics = compute_rates({}, {"eth2": {"rx_bytes": 5000, "tx_bytes": 3000,
                                          "rx_packets": 50, "tx_packets": 30,
                                          "rx_errors": 0, "tx_errors": 0,
                                          "rx_dropped": 0, "tx_dropped": 0,
                                          "operstate": "up"}}, interval_s=2.0)
    assert metrics[0]["rx_bps"] == 0.0


# ---------------------------------------------------------------------------
# write_to_redis
# ---------------------------------------------------------------------------


def test_write_to_redis_creates_stream(redis_client):
    """write_to_redis creates a stream entry under the topology key."""
    interfaces = [{"name": "e1-1", "rx_bps": 1000.0, "tx_bps": 500.0,
                   "state": "up", "source": "vxlan"}]

    write_to_redis(redis_client, "clab", "my-topo", "R1", interfaces)

    keys = redis_client.keys("nm:*")
    stream_key = "nm:topo:clab:my-topo:R1:e1-1"
    assert stream_key in keys

    entries = redis_client.xrange(stream_key)
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["rx_bps"] == "1000.0"
    assert fields["tx_bps"] == "500.0"
    assert fields["state"] == "up"
    assert "name" not in fields   # name is the key, not a field


def test_write_to_redis_index_sets(redis_client):
    """write_to_redis populates index Sets."""
    interfaces = [
        {"name": "e1-1", "rx_bps": 0.0, "tx_bps": 0.0, "state": "up", "source": "vxlan"},
        {"name": "e1-2", "rx_bps": 0.0, "tx_bps": 0.0, "state": "up", "source": "vxlan"},
    ]
    write_to_redis(redis_client, "clab", "my-topo", "R1", interfaces)

    assert redis_client.smembers("nm:topo:clab:my-topo:R1:ifaces") == {"e1-1", "e1-2"}
    assert redis_client.smembers("nm:topo:clab:my-topo:nodes") == {"R1"}
    assert redis_client.smembers("nm:topologies") == {"clab/my-topo"}


def test_write_to_redis_multiple_nodes(redis_client):
    """Multiple nodes accumulate into the same topology index."""
    ifaces_r1 = [{"name": "e1-1", "rx_bps": 0.0, "state": "up", "source": "vxlan"}]
    ifaces_r2 = [{"name": "e1-1", "rx_bps": 0.0, "state": "up", "source": "vxlan"}]

    write_to_redis(redis_client, "clab", "my-topo", "R1", ifaces_r1)
    write_to_redis(redis_client, "clab", "my-topo", "R2", ifaces_r2)

    assert redis_client.smembers("nm:topo:clab:my-topo:nodes") == {"R1", "R2"}


def test_write_to_redis_noop_when_no_client():
    """write_to_redis is a no-op when redis_client is None."""
    write_to_redis(None, "clab", "my-topo", "R1", [{"name": "e1-1"}])


def test_write_to_redis_handles_redis_error(redis_client):
    """Redis write failure is caught silently."""
    redis_client.pipeline = MagicMock(side_effect=Exception("connection lost"))
    write_to_redis(redis_client, "clab", "my-topo", "R1",
                   [{"name": "e1-1", "rx_bps": 0.0}])


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _start_test_server(node_id: str, store: dict, lock: threading.Lock) -> tuple[int, object]:
    """Start a test HTTP server on a random port. Returns (port, server)."""
    with patch("sidecar.agent.metrics_store", store), \
         patch("sidecar.agent.metrics_lock", lock):
        from http.server import HTTPServer
        handler = make_handler(node_id)
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port, server


def _http_get(port: int, path: str) -> tuple[int, bytes]:
    """Simple HTTP GET using raw socket."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", path)
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_http_interfaces_endpoint():
    """GET /interfaces returns node_id and interfaces list."""
    store = {
        "e1-1": {"name": "e1-1", "rx_bps": 1234.5, "tx_bps": 567.8,
                 "state": "up", "source": "vxlan"},
    }
    lock = threading.Lock()
    from http.server import HTTPServer
    handler = make_handler("clab/my-topo/R1", store=store, lock=lock)
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    status, body = _http_get(port, "/interfaces")
    server.shutdown()

    assert status == 200
    data = json.loads(body)
    assert data["node_id"] == "clab/my-topo/R1"
    assert len(data["interfaces"]) == 1
    assert data["interfaces"][0]["name"] == "e1-1"
    assert data["interfaces"][0]["rx_bps"] == 1234.5


def test_http_metrics_path_alias():
    """GET /metrics is an alias for /interfaces."""
    store = {"eth0": {"name": "eth0", "rx_bps": 0.0, "state": "up"}}
    lock = threading.Lock()
    from http.server import HTTPServer
    handler = make_handler("ns/topo/node", store=store, lock=lock)
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    status, body = _http_get(port, "/metrics")
    server.shutdown()

    assert status == 200
    assert json.loads(body)["node_id"] == "ns/topo/node"


def test_http_health_endpoint():
    """GET /health returns 200 with status ok."""
    from http.server import HTTPServer
    handler = make_handler("ns/topo/node", store={}, lock=threading.Lock())
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    status, body = _http_get(port, "/health")
    server.shutdown()

    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_http_unknown_path_returns_404():
    """GET on unknown path returns 404."""
    from http.server import HTTPServer
    handler = make_handler("ns/topo/node", store={}, lock=threading.Lock())
    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    status, _ = _http_get(port, "/unknown")
    server.shutdown()

    assert status == 404


# ---------------------------------------------------------------------------
# VXLAN name mapping
# ---------------------------------------------------------------------------


def test_vxlan_name_mapped_to_logical():
    """VXLAN interface names are replaced with logical names from Connectivity CR."""
    from sidecar.connectivity import VxlanLink, ConnectivityResolver

    mock_link = VxlanLink(
        vni=100, node="R1", logical_iface="e1-1-c1-1",
        remote_node="R2", remote_iface="e1-1-c1-1",
        vxlan_iface="vx-R1-e1-1-c1-1",
    )

    mock_connectivity = MagicMock(spec=ConnectivityResolver)
    mock_connectivity.resolve_vxlan.return_value = mock_link

    # Simulate the mapping logic from agent.py main loop
    rates = [{"name": "vx-R1-e1-1-c1-1", "rx_bps": 500.0, "state": "up"}]
    mapped = []
    for iface_dict in rates:
        iface_dict = dict(iface_dict)
        if iface_dict["name"].startswith("vx-"):
            link = mock_connectivity.resolve_vxlan(iface_dict["name"])
            if link:
                iface_dict["name"] = link.logical_iface
                iface_dict["source"] = "vxlan"
            else:
                iface_dict["source"] = "vxlan_raw"
        else:
            iface_dict["source"] = "kernel"
        mapped.append(iface_dict)

    assert mapped[0]["name"] == "e1-1-c1-1"
    assert mapped[0]["source"] == "vxlan"
    assert mapped[0]["rx_bps"] == 500.0


def test_vxlan_unknown_kept_as_vxlan_raw():
    """VXLAN interfaces with no CR match get source=vxlan_raw and keep raw name."""
    from sidecar.connectivity import ConnectivityResolver

    mock_connectivity = MagicMock(spec=ConnectivityResolver)
    mock_connectivity.resolve_vxlan.return_value = None

    rates = [{"name": "vx-R1-eth99", "rx_bps": 0.0, "state": "down"}]
    mapped = []
    for iface_dict in rates:
        iface_dict = dict(iface_dict)
        if iface_dict["name"].startswith("vx-"):
            link = mock_connectivity.resolve_vxlan(iface_dict["name"])
            if link:
                iface_dict["name"] = link.logical_iface
                iface_dict["source"] = "vxlan"
            else:
                iface_dict["source"] = "vxlan_raw"
        else:
            iface_dict["source"] = "kernel"
        mapped.append(iface_dict)

    assert mapped[0]["name"] == "vx-R1-eth99"
    assert mapped[0]["source"] == "vxlan_raw"


def test_non_vxlan_interface_gets_kernel_source():
    """Non-VXLAN interfaces get source=kernel."""
    from sidecar.connectivity import ConnectivityResolver

    mock_connectivity = MagicMock(spec=ConnectivityResolver)

    rates = [{"name": "docker0", "rx_bps": 0.0, "state": "up"}]
    mapped = []
    for iface_dict in rates:
        iface_dict = dict(iface_dict)
        if iface_dict["name"].startswith("vx-"):
            link = mock_connectivity.resolve_vxlan(iface_dict["name"])
            if link:
                iface_dict["name"] = link.logical_iface
                iface_dict["source"] = "vxlan"
            else:
                iface_dict["source"] = "vxlan_raw"
        else:
            iface_dict["source"] = "kernel"
        mapped.append(iface_dict)

    assert mapped[0]["source"] == "kernel"
    mock_connectivity.resolve_vxlan.assert_not_called()


# ---------------------------------------------------------------------------
# main() validation
# ---------------------------------------------------------------------------


def test_main_requires_pod_namespace():
    """main() exits if POD_NAMESPACE is missing."""
    env = {"TOPOLOGY_NAME": "my-topo", "NODE_NAME": "R1"}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()


def test_main_requires_topology_name():
    """main() exits if TOPOLOGY_NAME is missing."""
    env = {"POD_NAMESPACE": "clab", "NODE_NAME": "R1"}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()


def test_main_requires_node_name():
    """main() exits if NODE_NAME is missing."""
    env = {"POD_NAMESPACE": "clab", "TOPOLOGY_NAME": "my-topo"}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()
