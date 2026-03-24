"""
Tests for the standalone collector (sidecar/collector.py).

Tests cover:
- read_proc_net_dev: parsing /proc/net/dev output
- compute_rates: rate computation with exclude filter
- get_pods: kubectl pod discovery
- push_metrics: HTTP push
- main: entry point validation
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from sidecar.collector import (
    read_proc_net_dev,
    get_pods,
)
from sidecar.common import compute_rates, push_metrics


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
    """All interfaces from /proc/net/dev get state='up'."""
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
    """Parses kubectl output into pod list."""
    result = MagicMock()
    result.stdout = "pod1 clab\\npod2 clab"

    with patch("sidecar.collector.subprocess.run", return_value=result):
        pods = get_pods("clab", "clabernetes/app=clabernetes")

    assert len(pods) == 2
    assert pods[0] == {"name": "pod1", "namespace": "clab"}
    assert pods[1] == {"name": "pod2", "namespace": "clab"}


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
# push_metrics
# ---------------------------------------------------------------------------


def test_push_metrics_payload():
    """Sends correct JSON payload to API."""
    captured = {}

    def mock_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data)
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: resp
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    interfaces = [{"name": "eth0", "rx_bps": 100}]

    with patch("sidecar.common.urlopen", mock_urlopen):
        push_metrics("http://api:8000", "clab/pod1", interfaces, 2000)

    assert captured["url"] == "http://api:8000/api/interfaces"
    assert captured["data"]["node_id"] == "clab/pod1"
    assert captured["data"]["data_source"] == "sysfs"


def test_push_metrics_handles_error():
    """URLError is caught gracefully."""
    from urllib.error import URLError

    with patch("sidecar.common.urlopen", side_effect=URLError("refused")):
        push_metrics("http://api:8000", "clab/pod1", [], 2000)


# ---------------------------------------------------------------------------
# main() validation
# ---------------------------------------------------------------------------


def test_main_requires_api_url():
    """main() exits if API_URL is not set."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(SystemExit):
            from sidecar.collector import main
            main()
