"""
Tests for sidecar/connectivity.py.

Tests cover:
- VxlanLink dataclass
- ConnectivityResolver.refresh() with mocked K8s API
- resolve_vxlan(): maps vx-{node}-{iface} → VxlanLink
- Graceful failure when K8s API is unavailable
- Only tunnels where localNode matches node_name are indexed
"""

from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sidecar.connectivity import ConnectivityResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cr(tunnels: list[dict]) -> dict:
    """Build a minimal Connectivity CR response."""
    return {"spec": {"tunnels": tunnels}}


def _make_resolver(node_name: str = "R1") -> ConnectivityResolver:
    return ConnectivityResolver(
        namespace="clab",
        topology="my-topo",
        node_name=node_name,
    )


def _inject_mock_client(resolver: ConnectivityResolver, cr: dict):
    """Inject a mock K8s custom objects client that returns cr."""
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object.return_value = cr
    resolver._custom = mock_custom


# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------


def test_resolve_vxlan_basic():
    """vx-R1-e1-1-c1-1 resolves to logical_iface=e1-1-c1-1 for node R1."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {
            "tunnelID": 100,
            "localNode": "R1",
            "localInterface": "e1-1-c1-1",
            "remoteNode": "R2",
            "remoteInterface": "e1-1-c1-1",
        }
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    link = resolver.resolve_vxlan("vx-R1-e1-1-c1-1")
    assert link is not None
    assert link.node == "R1"
    assert link.logical_iface == "e1-1-c1-1"
    assert link.vxlan_iface == "vx-R1-e1-1-c1-1"
    assert link.vni == 100
    assert link.remote_node == "R2"
    assert link.remote_iface == "e1-1-c1-1"


def test_resolve_vxlan_simple_iface():
    """vx-R1-eth1 resolves correctly for a simple interface name."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {
            "tunnelID": 42,
            "localNode": "R1",
            "localInterface": "eth1",
            "remoteNode": "R2",
            "remoteInterface": "eth1",
        }
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    link = resolver.resolve_vxlan("vx-R1-eth1")
    assert link is not None
    assert link.logical_iface == "eth1"
    assert link.vni == 42


def test_resolve_vxlan_unknown_returns_none():
    """Returns None for an interface not in the CR."""
    resolver = _make_resolver("R1")
    _inject_mock_client(resolver, _make_cr([]))
    resolver.refresh()

    assert resolver.resolve_vxlan("vx-R1-eth0") is None


# ---------------------------------------------------------------------------
# node_name filtering
# ---------------------------------------------------------------------------


def test_only_local_node_tunnels_indexed():
    """Tunnels where localNode != node_name are not indexed."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {
            "tunnelID": 1,
            "localNode": "R1",
            "localInterface": "e1-1",
            "remoteNode": "R2",
            "remoteInterface": "e1-1",
        },
        {
            "tunnelID": 2,
            "localNode": "R2",      # different node
            "localInterface": "e1-2",
            "remoteNode": "R1",
            "remoteInterface": "e1-2",
        },
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    assert resolver.resolve_vxlan("vx-R1-e1-1") is not None   # R1's tunnel
    assert resolver.resolve_vxlan("vx-R2-e1-2") is None       # R2's tunnel, not indexed


def test_multiple_tunnels_for_same_node():
    """All tunnels where localNode == node_name are indexed."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {"tunnelID": 10, "localNode": "R1", "localInterface": "e1-1",
         "remoteNode": "R2", "remoteInterface": "e1-1"},
        {"tunnelID": 20, "localNode": "R1", "localInterface": "e1-2",
         "remoteNode": "R3", "remoteInterface": "e1-1"},
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    assert resolver.link_count == 2
    assert resolver.resolve_vxlan("vx-R1-e1-1") is not None
    assert resolver.resolve_vxlan("vx-R1-e1-2") is not None


# ---------------------------------------------------------------------------
# vxlan_ifaces property
# ---------------------------------------------------------------------------


def test_vxlan_ifaces_property():
    """vxlan_ifaces returns the set of known VXLAN interface names."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {"tunnelID": 1, "localNode": "R1", "localInterface": "e1-1",
         "remoteNode": "R2", "remoteInterface": "e1-1"},
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    assert resolver.vxlan_ifaces == {"vx-R1-e1-1"}


# ---------------------------------------------------------------------------
# Graceful failure
# ---------------------------------------------------------------------------


def test_refresh_k8s_unavailable_keeps_empty_map():
    """If K8s API fails on first refresh, map stays empty (no crash)."""
    resolver = _make_resolver("R1")
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object.side_effect = Exception("connection refused")
    resolver._custom = mock_custom

    resolver.refresh()   # should not raise

    assert resolver.link_count == 0
    assert resolver.resolve_vxlan("vx-R1-e1-1") is None


def test_refresh_k8s_unavailable_preserves_existing_map():
    """If K8s API fails on subsequent refresh, existing map is preserved."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {"tunnelID": 5, "localNode": "R1", "localInterface": "e1-1",
         "remoteNode": "R2", "remoteInterface": "e1-1"},
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()
    assert resolver.link_count == 1

    # Simulate API failure on second refresh
    resolver._custom.get_namespaced_custom_object.side_effect = Exception("timeout")
    resolver.refresh()   # should not raise or clear the map

    assert resolver.link_count == 1     # old map preserved
    assert resolver.resolve_vxlan("vx-R1-e1-1") is not None


def test_refresh_missing_kubernetes_package():
    """RuntimeError from missing kubernetes package is caught gracefully."""
    resolver = _make_resolver("R1")
    # Don't inject a client — let it try to import kubernetes

    with patch("sidecar.connectivity.ConnectivityResolver._init_client",
               side_effect=RuntimeError("No module named 'kubernetes'")):
        resolver.refresh()   # should warn and return, not raise

    assert resolver.link_count == 0


# ---------------------------------------------------------------------------
# Tunnel spec validation
# ---------------------------------------------------------------------------


def test_incomplete_tunnel_skipped():
    """Tunnels missing required fields are skipped."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {"tunnelID": 1, "localNode": "R1", "localInterface": "e1-1",
         "remoteNode": "R2"},   # missing remoteInterface
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    assert resolver.link_count == 0


def test_zero_vni_tunnel_skipped():
    """Tunnels with tunnelID=0 are skipped."""
    resolver = _make_resolver("R1")
    cr = _make_cr([
        {"tunnelID": 0, "localNode": "R1", "localInterface": "e1-1",
         "remoteNode": "R2", "remoteInterface": "e1-1"},
    ])
    _inject_mock_client(resolver, cr)
    resolver.refresh()

    assert resolver.link_count == 0
