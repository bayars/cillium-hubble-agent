"""
Tests for HubbleMonitor flow parsing and state tracking logic.

These tests validate the internal logic without requiring a real Hubble Relay.
"""

import pytest

from api.services.hubble_monitor import (
    HubbleMonitor,
    Endpoint,
    FlowEvent,
    FlowVerdict,
    TrafficDirection,
    LinkState,
    FlowMetrics,
)


@pytest.fixture
def monitor():
    """Create a HubbleMonitor instance (no connection needed for unit tests)."""
    m = HubbleMonitor.__new__(HubbleMonitor)
    m.relay_addr = "test:4245"
    m.idle_timeout = __import__("datetime").timedelta(seconds=5)
    m.callback = None
    m.use_tls = False
    m._channel = None
    m._stub = None
    m._running = False
    m._flow_last_seen = {}
    m._flow_states = {}
    m._flow_endpoints = {}
    m._flow_metrics = {}
    m._event_queue = __import__("asyncio").Queue()
    m._flow_count_window = []
    m._rate_window_seconds = 10.0
    m._idle_check_task = None
    return m


def _make_flow(
    src_ns="default",
    src_pod="pod-a",
    dst_ns="default",
    dst_pod="pod-b",
    verdict=FlowVerdict.FORWARDED,
    protocol="TCP",
) -> FlowEvent:
    """Helper to create a FlowEvent."""
    return FlowEvent(
        source=Endpoint(namespace=src_ns, pod_name=src_pod),
        destination=Endpoint(namespace=dst_ns, pod_name=dst_pod),
        verdict=verdict,
        direction=TrafficDirection.EGRESS,
        l4_protocol=protocol,
        source_port=54321,
        destination_port=80,
    )


def test_flow_key():
    """Test flow key generation."""
    flow = _make_flow()
    assert flow.flow_key == "default/pod-a->default/pod-b"


def test_endpoint_id_with_namespace():
    ep = Endpoint(namespace="kube-system", pod_name="coredns-abc123")
    assert ep.id == "kube-system/coredns-abc123"


def test_endpoint_id_fallback_to_ip():
    ep = Endpoint(ip="10.0.0.5")
    assert ep.id == "10.0.0.5"


def test_endpoint_id_fallback_to_identity():
    ep = Endpoint(identity=12345)
    assert ep.id == "identity:12345"


def test_update_flow_state_new_forwarded(monitor):
    """First FORWARDED flow should transition from UNKNOWN to ACTIVE."""
    flow = _make_flow(verdict=FlowVerdict.FORWARDED)
    change = monitor._update_flow_state(flow)

    assert change is not None
    assert change.old_state == LinkState.UNKNOWN
    assert change.new_state == LinkState.ACTIVE
    assert change.flow_key == "default/pod-a->default/pod-b"


def test_update_flow_state_dropped(monitor):
    """DROPPED verdict should set state to DOWN."""
    flow = _make_flow(verdict=FlowVerdict.DROPPED)
    change = monitor._update_flow_state(flow)

    assert change is not None
    assert change.new_state == LinkState.DOWN


def test_no_state_change_on_same_verdict(monitor):
    """Repeated FORWARDED flows should not trigger state change."""
    flow = _make_flow(verdict=FlowVerdict.FORWARDED)

    # First flow: UNKNOWN -> ACTIVE
    change1 = monitor._update_flow_state(flow)
    assert change1 is not None

    # Second flow: still ACTIVE, no change
    change2 = monitor._update_flow_state(flow)
    assert change2 is None


def test_state_transition_active_to_down(monitor):
    """Flow going from FORWARDED to DROPPED should transition ACTIVE -> DOWN."""
    flow_ok = _make_flow(verdict=FlowVerdict.FORWARDED)
    flow_drop = _make_flow(verdict=FlowVerdict.DROPPED)

    monitor._update_flow_state(flow_ok)
    change = monitor._update_flow_state(flow_drop)

    assert change is not None
    assert change.old_state == LinkState.ACTIVE
    assert change.new_state == LinkState.DOWN


def test_flow_metrics_tracked(monitor):
    """Flow metrics should be updated with each flow event."""
    flow = _make_flow(verdict=FlowVerdict.FORWARDED, protocol="TCP")
    monitor._update_flow_state(flow)

    key = flow.flow_key
    metrics = monitor._flow_metrics.get(key)
    assert metrics is not None
    assert metrics.flows_total == 1
    assert metrics.flows_forwarded == 1
    assert metrics.flows_dropped == 0
    assert metrics.protocols == {"TCP": 1}

    # Second flow
    monitor._update_flow_state(flow)
    assert metrics.flows_total == 2
    assert metrics.flows_forwarded == 2


def test_flow_metrics_count_dropped(monitor):
    """Dropped flows should increment flows_dropped."""
    flow = _make_flow(verdict=FlowVerdict.DROPPED)
    monitor._update_flow_state(flow)

    key = flow.flow_key
    metrics = monitor._flow_metrics[key]
    assert metrics.flows_dropped == 1
    assert metrics.flows_forwarded == 0


def test_flow_metrics_protocol_breakdown(monitor):
    """Multiple protocols should be tracked separately."""
    tcp_flow = _make_flow(protocol="TCP")
    udp_flow = _make_flow(protocol="UDP")

    monitor._update_flow_state(tcp_flow)
    monitor._update_flow_state(tcp_flow)
    monitor._update_flow_state(udp_flow)

    key = tcp_flow.flow_key
    metrics = monitor._flow_metrics[key]
    assert metrics.protocols == {"TCP": 2, "UDP": 1}


def test_multiple_flow_keys_tracked(monitor):
    """Different src/dst pairs should be tracked independently."""
    flow_ab = _make_flow(src_pod="pod-a", dst_pod="pod-b")
    flow_cd = _make_flow(src_pod="pod-c", dst_pod="pod-d")

    change_ab = monitor._update_flow_state(flow_ab)
    change_cd = monitor._update_flow_state(flow_cd)

    assert change_ab is not None
    assert change_cd is not None
    assert len(monitor._flow_states) == 2
    assert len(monitor._flow_metrics) == 2


def test_change_includes_metrics(monitor):
    """LinkStateChange should include current FlowMetrics."""
    flow = _make_flow()
    change = monitor._update_flow_state(flow)

    assert change.metrics is not None
    assert change.metrics.flows_total == 1


def test_get_active_flows(monitor):
    """get_active_flows should return only active flow keys."""
    flow_active = _make_flow(src_pod="a", dst_pod="b", verdict=FlowVerdict.FORWARDED)
    flow_down = _make_flow(src_pod="c", dst_pod="d", verdict=FlowVerdict.DROPPED)

    monitor._update_flow_state(flow_active)
    monitor._update_flow_state(flow_down)

    active = monitor.get_active_flows()
    assert "default/a->default/b" in active
    assert "default/c->default/d" not in active


def test_flow_metrics_to_dict():
    """Test FlowMetrics serialization."""
    m = FlowMetrics(
        flows_total=100,
        flows_forwarded=95,
        flows_dropped=5,
        flows_per_second=10.0,
        active_connections=3,
        protocols={"TCP": 80, "UDP": 20},
    )
    d = m.to_dict()
    assert d["flows_total"] == 100
    assert d["flows_per_second"] == 10.0
    assert d["protocols"] == {"TCP": 80, "UDP": 20}
