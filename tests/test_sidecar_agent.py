"""
Tests for the sidecar agent (sidecar/agent.py) and shared utilities.

Tests cover:
- discover_interfaces: listing network interfaces from sysfs
- read_counter: reading individual sysfs counter files
- get_operstate: reading interface operational state
- read_all_counters: bulk counter reads
- compute_rates: rate computation from two counter snapshots
- write_to_redis: Redis Streams write with fakeredis
- HTTP server: GET /interfaces and /health responses
- main: entry point validation
"""

import json
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from sidecar.agent import (
    discover_interfaces,
    read_counter,
    get_operstate,
    read_all_counters,
)
from sidecar.common import compute_rates, write_to_redis


# ---------------------------------------------------------------------------
# discover_interfaces
# ---------------------------------------------------------------------------


def test_discover_interfaces(tmp_path):
    """Discovers interfaces that have a statistics/ subdirectory."""
    for iface in ("eth0", "eth1", "lo"):
        iface_dir = tmp_path / iface
        iface_dir.mkdir()
        (iface_dir / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude={"lo"})
    assert sorted(result) == ["eth0", "eth1"]


def test_discover_interfaces_excludes_multiple(tmp_path):
    """Multiple interfaces can be excluded."""
    for iface in ("eth0", "eth1", "docker0", "lo"):
        d = tmp_path / iface
        d.mkdir()
        (d / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude={"lo", "docker0"})
    assert sorted(result) == ["eth0", "eth1"]


def test_discover_interfaces_no_statistics_dir(tmp_path):
    """Interfaces without statistics/ directory are skipped."""
    (tmp_path / "eth0").mkdir()  # no statistics subdir
    iface_with = tmp_path / "eth1"
    iface_with.mkdir()
    (iface_with / "statistics").mkdir()

    with patch("sidecar.agent.SYSFS_NET", tmp_path):
        result = discover_interfaces(exclude=set())
    assert result == ["eth1"]


def test_discover_interfaces_sysfs_missing(tmp_path):
    """Returns empty list when /sys/class/net doesn't exist."""
    missing = tmp_path / "nonexistent"
    with patch("sidecar.agent.SYSFS_NET", missing):
        result = discover_interfaces(exclude=set())
    assert result == []


# ---------------------------------------------------------------------------
# read_counter
# ---------------------------------------------------------------------------


def test_read_counter(tmp_path):
    """Reads an integer from a sysfs counter file."""
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_bytes").write_text("123456\n")

    assert read_counter(tmp_path, "rx_bytes") == 123456


def test_read_counter_missing_file(tmp_path):
    """Returns 0 when counter file doesn't exist."""
    assert read_counter(tmp_path, "rx_bytes") == 0


def test_read_counter_invalid_content(tmp_path):
    """Returns 0 when counter file has non-integer content."""
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_bytes").write_text("not_a_number\n")

    assert read_counter(tmp_path, "rx_bytes") == 0


# ---------------------------------------------------------------------------
# get_operstate
# ---------------------------------------------------------------------------


def test_get_operstate_up(tmp_path):
    """Returns 'up' when operstate file reads 'up'."""
    (tmp_path / "operstate").write_text("up\n")
    assert get_operstate(tmp_path) == "up"


def test_get_operstate_down(tmp_path):
    """Returns 'down' for any non-'up' value."""
    (tmp_path / "operstate").write_text("down\n")
    assert get_operstate(tmp_path) == "down"


def test_get_operstate_unknown_value(tmp_path):
    """Returns 'down' for unrecognized state like 'dormant'."""
    (tmp_path / "operstate").write_text("dormant\n")
    assert get_operstate(tmp_path) == "down"


def test_get_operstate_missing_file(tmp_path):
    """Returns 'unknown' when operstate file is missing."""
    assert get_operstate(tmp_path) == "unknown"


# ---------------------------------------------------------------------------
# read_all_counters
# ---------------------------------------------------------------------------


def _make_sysfs_iface(base: Path, name: str, counters: dict, operstate: str = "up"):
    """Helper: create a fake sysfs interface directory."""
    iface_dir = base / name
    iface_dir.mkdir(exist_ok=True)
    stats = iface_dir / "statistics"
    stats.mkdir(exist_ok=True)
    for counter, value in counters.items():
        (stats / counter).write_text(str(value) + "\n")
    (iface_dir / "operstate").write_text(operstate + "\n")


def test_read_all_counters(tmp_path):
    """Reads all counters for all specified interfaces."""
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
    """Computes per-second rates from two snapshots."""
    prev = {
        "eth0": {
            "rx_bytes": 1000, "tx_bytes": 500,
            "rx_packets": 100, "tx_packets": 50,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }
    curr = {
        "eth0": {
            "rx_bytes": 3000, "tx_bytes": 1500,
            "rx_packets": 200, "tx_packets": 100,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }

    metrics = compute_rates(prev, curr, interval_s=2.0)
    assert len(metrics) == 1
    m = metrics[0]
    assert m["name"] == "eth0"
    assert m["rx_bps"] == 1000.0  # (3000-1000) / 2
    assert m["tx_bps"] == 500.0   # (1500-500) / 2
    assert m["rx_pps"] == 50.0    # (200-100) / 2
    assert m["tx_pps"] == 25.0    # (100-50) / 2


def test_compute_rates_no_previous():
    """First snapshot: rates should be zero."""
    curr = {
        "eth0": {
            "rx_bytes": 1000, "tx_bytes": 500,
            "rx_packets": 10, "tx_packets": 5,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }

    metrics = compute_rates({}, curr, interval_s=2.0)
    assert len(metrics) == 1
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["tx_bps"] == 0.0


def test_compute_rates_counter_wrap():
    """Counter wrap (decrease) should clamp to zero, not go negative."""
    prev = {
        "eth0": {
            "rx_bytes": 5000, "tx_bytes": 3000,
            "rx_packets": 100, "tx_packets": 50,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }
    curr = {
        "eth0": {
            "rx_bytes": 1000, "tx_bytes": 500,  # wrapped
            "rx_packets": 100, "tx_packets": 50,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }

    metrics = compute_rates(prev, curr, interval_s=1.0)
    assert metrics[0]["rx_bps"] == 0.0
    assert metrics[0]["tx_bps"] == 0.0


def test_compute_rates_zero_interval():
    """Zero interval should not cause division by zero."""
    prev = {
        "eth0": {
            "rx_bytes": 1000, "tx_bytes": 500,
            "rx_packets": 10, "tx_packets": 5,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }
    curr = dict(prev)

    metrics = compute_rates(prev, curr, interval_s=0)
    assert metrics[0]["rx_bps"] == 0.0


def test_compute_rates_multiple_interfaces():
    """Handles multiple interfaces in one call."""
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
    assert len(metrics) == 2
    by_name = {m["name"]: m for m in metrics}
    assert by_name["eth0"]["rx_bps"] == 2000.0
    assert by_name["eth1"]["rx_bps"] == 4000.0


def test_compute_rates_includes_totals():
    """Output includes absolute total counters."""
    curr = {
        "eth0": {
            "rx_bytes": 99999, "tx_bytes": 88888,
            "rx_packets": 1000, "tx_packets": 900,
            "rx_errors": 5, "tx_errors": 3,
            "rx_dropped": 2, "tx_dropped": 1,
            "operstate": "up",
        },
    }

    metrics = compute_rates({}, curr, interval_s=1.0)
    m = metrics[0]
    assert m["rx_bytes_total"] == 99999
    assert m["tx_bytes_total"] == 88888
    assert m["rx_errors"] == 5
    assert m["tx_errors"] == 3
    assert m["rx_dropped"] == 2
    assert m["tx_dropped"] == 1


def test_compute_rates_new_interface_in_curr():
    """A new interface appearing in curr but not in prev gets zero rates."""
    prev = {}
    curr = {
        "eth2": {
            "rx_bytes": 5000, "tx_bytes": 3000,
            "rx_packets": 50, "tx_packets": 30,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }

    metrics = compute_rates(prev, curr, interval_s=2.0)
    assert len(metrics) == 1
    assert metrics[0]["name"] == "eth2"
    assert metrics[0]["rx_bps"] == 0.0


# ---------------------------------------------------------------------------
# write_to_redis
# ---------------------------------------------------------------------------


def test_write_to_redis_streams_data(redis_client):
    """write_to_redis creates a Stream under nm:topo:{ns}:{topo}:{node}:{iface}."""
    interfaces = [
        {"name": "eth0", "rx_bps": 1000.0, "tx_bps": 500.0,
         "rx_bytes_total": 10000, "tx_bytes_total": 5000,
         "rx_packets_total": 100, "tx_packets_total": 50,
         "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
         "state": "up", "rx_pps": 10.0, "tx_pps": 5.0},
    ]

    write_to_redis(redis_client, "default/srl-probe-test/srl1", interfaces)

    entries = redis_client.xrange("nm:topo:default:srl-probe-test:srl1:eth0")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["rx_bps"] == "1000.0"
    assert fields["tx_bps"] == "500.0"
    assert "name" not in fields  # name is the key suffix, not a field


def test_write_to_redis_indexes_node(redis_client):
    """write_to_redis adds node to topology node-set and iface to iface-set."""
    interfaces = [{"name": "eth0", "rx_bps": 0.0, "tx_bps": 0.0,
                   "rx_bytes_total": 0, "tx_bytes_total": 0,
                   "rx_packets_total": 0, "tx_packets_total": 0,
                   "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
                   "state": "up", "rx_pps": 0.0, "tx_pps": 0.0}]

    write_to_redis(redis_client, "default/dc1/leaf1", interfaces)

    assert redis_client.sismember("nm:topologies", "default/dc1")
    assert redis_client.sismember("nm:topo:default:dc1:nodes", "leaf1")
    assert redis_client.sismember("nm:topo:default:dc1:leaf1:ifaces", "eth0")


def test_write_to_redis_multiple_interfaces(redis_client):
    """write_to_redis handles multiple interfaces in one call."""
    interfaces = [
        {"name": "eth0", "rx_bps": 100.0, "tx_bps": 50.0,
         "rx_bytes_total": 0, "tx_bytes_total": 0, "rx_packets_total": 0, "tx_packets_total": 0,
         "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
         "state": "up", "rx_pps": 0.0, "tx_pps": 0.0},
        {"name": "eth1", "rx_bps": 200.0, "tx_bps": 100.0,
         "rx_bytes_total": 0, "tx_bytes_total": 0, "rx_packets_total": 0, "tx_packets_total": 0,
         "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0,
         "state": "up", "rx_pps": 0.0, "tx_pps": 0.0},
    ]

    write_to_redis(redis_client, "default/srl-probe-test/r1", interfaces)

    assert len(redis_client.xrange("nm:topo:default:srl-probe-test:r1:eth0")) == 1
    assert len(redis_client.xrange("nm:topo:default:srl-probe-test:r1:eth1")) == 1
    ifaces = redis_client.smembers("nm:topo:default:srl-probe-test:r1:ifaces")
    assert ifaces == {"eth0", "eth1"}


def test_write_to_redis_none_client():
    """write_to_redis is a no-op when redis_client is None."""
    # Should not raise
    write_to_redis(None, "clab/r1", [{"name": "eth0", "rx_bps": 0.0}])


def test_write_to_redis_redis_error(redis_client, monkeypatch):
    """write_to_redis silently catches Redis errors."""
    def boom(*args, **kwargs):
        raise Exception("connection lost")

    monkeypatch.setattr(redis_client, "pipeline", boom)
    # Should not raise
    write_to_redis(redis_client, "default/srl-probe-test/r1", [{"name": "eth0", "rx_bps": 0.0}])


def test_write_to_redis_bad_node_id(redis_client):
    """write_to_redis skips write when node_id is not in ns/topology/node format."""
    write_to_redis(redis_client, "bad-id", [{"name": "eth0"}])
    assert redis_client.keys("nm:*") == []


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


def _start_test_server(node: str, iface_data: list[dict]) -> int:
    """Start the sidecar HTTP server in a daemon thread and return the port."""
    import sidecar.agent as agent_mod
    from http.server import HTTPServer
    from sidecar.agent import MetricsHandler

    agent_mod.node_id = node
    with agent_mod.metrics_lock:
        agent_mod.metrics_store.clear()
        agent_mod.metrics_store.update({m["name"]: m for m in iface_data})

    server = HTTPServer(("127.0.0.1", 0), MetricsHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def test_http_interfaces_endpoint():
    """GET /interfaces returns node_id and interfaces JSON."""
    ifaces = [{"name": "eth0", "rx_bps": 999.0, "tx_bps": 111.0}]
    port = _start_test_server("clab/r1", ifaces)

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/interfaces")
    data = json.loads(resp.read())

    assert data["node_id"] == "clab/r1"
    assert len(data["interfaces"]) == 1
    assert data["interfaces"][0]["rx_bps"] == 999.0


def test_http_root_endpoint():
    """GET / returns the same response as /interfaces."""
    ifaces = [{"name": "eth1", "rx_bps": 42.0}]
    port = _start_test_server("clab/r2", ifaces)

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/")
    data = json.loads(resp.read())
    assert data["node_id"] == "clab/r2"


def test_http_metrics_endpoint():
    """GET /metrics returns the same response as /interfaces."""
    ifaces = [{"name": "eth0", "rx_bps": 0.0}]
    port = _start_test_server("clab/r3", ifaces)

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics")
    data = json.loads(resp.read())
    assert "interfaces" in data


def test_http_health_endpoint():
    """GET /health returns {"status": "ok"}."""
    port = _start_test_server("clab/r4", [])

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health")
    data = json.loads(resp.read())
    assert data["status"] == "ok"


def test_http_404():
    """Unknown paths return 404."""
    port = _start_test_server("clab/r5", [])

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown")
        assert False, "Expected HTTPError"
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


# ---------------------------------------------------------------------------
# main() validation
# ---------------------------------------------------------------------------


def test_main_requires_topology_env_vars():
    """main() exits if TOPOLOGY_NAME, NODE_NAME, or POD_NAMESPACE are missing."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()


def test_main_builds_node_id_from_labels(tmp_path):
    """main() builds node_id as namespace/topology/node from Clabernetes label env vars."""
    iface_dir = tmp_path / "eth0"
    iface_dir.mkdir()
    stats = iface_dir / "statistics"
    stats.mkdir()
    for c in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
              "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
        (stats / c).write_text("0\n")
    (iface_dir / "operstate").write_text("up\n")

    env = {
        "POD_NAMESPACE": "default",
        "TOPOLOGY_NAME": "srl-probe-test",
        "NODE_NAME": "srl1",
        "POLL_INTERVAL_MS": "1000",
        "EXCLUDE_IFACES": "lo",
        "API_PORT": "0",
    }

    call_count = {"n": 0}

    def mock_sleep(s):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise KeyboardInterrupt

    with patch.dict("os.environ", env, clear=True), \
         patch("sidecar.agent.SYSFS_NET", tmp_path), \
         patch("sidecar.agent.time.sleep", mock_sleep), \
         patch("sidecar.agent.write_to_redis"):
        try:
            import importlib
            import sidecar.agent
            importlib.reload(sidecar.agent)
            sidecar.agent.main()
        except KeyboardInterrupt:
            pass

    import sidecar.agent as a
    assert a.node_id == "default/srl-probe-test/srl1"
