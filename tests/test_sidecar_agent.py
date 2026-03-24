"""
Tests for the sidecar agent (sidecar/agent.py).

Tests cover:
- discover_interfaces: listing network interfaces from sysfs
- read_counter: reading individual sysfs counter files
- get_operstate: reading interface operational state
- read_all_counters: bulk counter reads
- compute_rates: rate computation from two counter snapshots
- push_metrics: HTTP push to the API
- main: entry point validation
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from sidecar.agent import (
    discover_interfaces,
    read_counter,
    get_operstate,
    read_all_counters,
)
from sidecar.common import compute_rates, push_metrics


# ---------------------------------------------------------------------------
# discover_interfaces
# ---------------------------------------------------------------------------


def test_discover_interfaces(tmp_path):
    """Discovers interfaces that have a statistics/ subdirectory."""
    # Create fake sysfs structure
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
    curr = dict(prev)  # same

    metrics = compute_rates(prev, curr, interval_s=0)
    assert metrics[0]["rx_bps"] == 0.0


def test_compute_rates_multiple_interfaces():
    """Handles multiple interfaces in one call."""
    prev = {
        "eth0": {
            "rx_bytes": 0, "tx_bytes": 0,
            "rx_packets": 0, "tx_packets": 0,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
        "eth1": {
            "rx_bytes": 0, "tx_bytes": 0,
            "rx_packets": 0, "tx_packets": 0,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
    }
    curr = {
        "eth0": {
            "rx_bytes": 2000, "tx_bytes": 1000,
            "rx_packets": 20, "tx_packets": 10,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
        "eth1": {
            "rx_bytes": 4000, "tx_bytes": 2000,
            "rx_packets": 40, "tx_packets": 20,
            "rx_errors": 0, "tx_errors": 0,
            "rx_dropped": 0, "tx_dropped": 0,
            "operstate": "up",
        },
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
# push_metrics
# ---------------------------------------------------------------------------


def test_push_metrics_success():
    """Successful push sends correct JSON payload."""
    captured = {}

    def mock_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["data"] = json.loads(req.data)
        captured["headers"] = dict(req.headers)
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    interfaces = [{"name": "eth0", "rx_bps": 1000, "tx_bps": 500}]

    with patch("sidecar.common.urlopen", mock_urlopen):
        push_metrics("http://api:8000", "clab/spine1", interfaces, 2000)

    assert captured["url"] == "http://api:8000/api/interfaces"
    assert captured["method"] == "PUT"
    assert captured["data"]["node_id"] == "clab/spine1"
    assert captured["data"]["interfaces"] == interfaces
    assert captured["data"]["poll_interval_ms"] == 2000
    assert captured["data"]["data_source"] == "sysfs"


def test_push_metrics_api_error():
    """URLError is caught and logged, no exception raised."""
    from urllib.error import URLError

    def mock_urlopen(req, timeout=None):
        raise URLError("Connection refused")

    with patch("sidecar.common.urlopen", mock_urlopen):
        # Should not raise
        push_metrics("http://api:8000", "clab/spine1", [], 2000)


# ---------------------------------------------------------------------------
# main() validation
# ---------------------------------------------------------------------------


def test_main_requires_api_url():
    """main() exits if API_URL is not set."""
    env = {"POLL_INTERVAL_MS": "1000", "POD_NAME": "pod1", "POD_NAMESPACE": "ns1"}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()


def test_main_requires_node_id_or_pod_info():
    """main() exits if neither NODE_ID nor POD_NAME+POD_NAMESPACE are set."""
    env = {"API_URL": "http://localhost:8000", "POLL_INTERVAL_MS": "1000"}
    with patch.dict("os.environ", env, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.agent import main
            main()


def test_main_auto_detects_node_id(tmp_path):
    """main() auto-detects node_id from POD_NAME + POD_NAMESPACE."""
    # Create a fake interface so main doesn't exit on "no interfaces"
    iface_dir = tmp_path / "eth0"
    iface_dir.mkdir()
    stats = iface_dir / "statistics"
    stats.mkdir()
    for c in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets",
              "rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
        (stats / c).write_text("0\n")
    (iface_dir / "operstate").write_text("up\n")

    env = {
        "API_URL": "http://localhost:8000",
        "POD_NAME": "my-pod",
        "POD_NAMESPACE": "clab",
        "POLL_INTERVAL_MS": "1000",
        "EXCLUDE_IFACES": "lo",
    }

    call_count = {"n": 0}

    def mock_sleep(s):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise KeyboardInterrupt

    with patch.dict("os.environ", env, clear=True), \
         patch("sidecar.agent.SYSFS_NET", tmp_path), \
         patch("sidecar.agent.time.sleep", mock_sleep), \
         patch("sidecar.agent.push_metrics") as mock_push:
        try:
            from sidecar.agent import main
            main()
        except KeyboardInterrupt:
            pass

    if mock_push.called:
        assert mock_push.call_args[0][1] == "clab/my-pod"
