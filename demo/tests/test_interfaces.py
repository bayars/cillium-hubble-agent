"""
Tests for per-interface metrics API (collector agent integration).
"""

import pytest
from httpx import AsyncClient

from api.models.schemas import InterfaceMetrics, InterfaceState
from .helpers import create_node


@pytest.mark.asyncio
async def test_push_interface_metrics(client: AsyncClient):
    """Collector pushes interface metrics for a node."""
    await create_node(client, "spine1")

    payload = {
        "node_id": "spine1",
        "interfaces": [
            {
                "name": "ethernet-1/1",
                "state": "up",
                "rx_bps": 5000000.0,
                "tx_bps": 1200000.0,
                "rx_pps": 4000.0,
                "tx_pps": 1000.0,
                "rx_bytes_total": 500000000,
                "tx_bytes_total": 120000000,
                "rx_packets_total": 400000,
                "tx_packets_total": 100000,
            },
            {
                "name": "ethernet-1/2",
                "state": "up",
                "rx_bps": 8800000.0,
                "tx_bps": 3300000.0,
            },
            {
                "name": "mgmt0",
                "state": "up",
                "rx_bps": 900.0,
                "tx_bps": 200.0,
            },
        ],
        "poll_interval_ms": 1000,
        "data_source": "sysfs",
    }

    response = await client.put("/api/interfaces", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["node_id"] == "spine1"
    assert data["count"] == 3

    ifaces = {i["name"]: i for i in data["interfaces"]}
    assert ifaces["ethernet-1/1"]["rx_bps"] == 5000000.0
    assert ifaces["ethernet-1/1"]["tx_bps"] == 1200000.0
    assert ifaces["ethernet-1/2"]["rx_bps"] == 8800000.0
    assert ifaces["mgmt0"]["state"] == "up"


@pytest.mark.asyncio
async def test_get_node_interfaces(client: AsyncClient):
    """GET returns all pushed interfaces."""
    await create_node(client, "leaf1")

    payload = {
        "node_id": "leaf1",
        "interfaces": [
            {"name": "eth0", "state": "up", "rx_bps": 100.0, "tx_bps": 50.0},
            {"name": "eth1", "state": "down", "rx_bps": 0.0, "tx_bps": 0.0},
        ],
    }
    await client.put("/api/interfaces", json=payload)

    response = await client.get("/api/interfaces", params={"node_id": "leaf1"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2

    ifaces = {i["name"]: i for i in data["interfaces"]}
    assert ifaces["eth0"]["state"] == "up"
    assert ifaces["eth1"]["state"] == "down"


@pytest.mark.asyncio
async def test_push_updates_existing(client: AsyncClient):
    """Subsequent pushes update existing interface metrics."""
    await create_node(client, "sw1")

    payload1 = {
        "node_id": "sw1",
        "interfaces": [{"name": "eth0", "state": "up", "rx_bps": 100.0}],
    }
    await client.put("/api/interfaces", json=payload1)

    payload2 = {
        "node_id": "sw1",
        "interfaces": [{"name": "eth0", "state": "up", "rx_bps": 5000.0}],
    }
    await client.put("/api/interfaces", json=payload2)

    response = await client.get("/api/interfaces", params={"node_id": "sw1"})
    ifaces = {i["name"]: i for i in response.json()["interfaces"]}
    assert ifaces["eth0"]["rx_bps"] == 5000.0


@pytest.mark.asyncio
async def test_push_to_nonexistent_node(client: AsyncClient):
    """Push to nonexistent node returns 404."""
    payload = {
        "node_id": "does-not-exist",
        "interfaces": [{"name": "eth0", "state": "up"}],
    }
    response = await client.put("/api/interfaces", json=payload)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_interfaces_nonexistent_node(client: AsyncClient):
    """GET interfaces for nonexistent node returns 404."""
    response = await client.get("/api/interfaces", params={"node_id": "no-such-node"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_interface_with_errors_and_drops(client: AsyncClient):
    """Interface error and drop counters are stored."""
    await create_node(client, "r3")

    payload = {
        "node_id": "r3",
        "interfaces": [
            {
                "name": "eth0",
                "state": "up",
                "rx_errors": 5,
                "tx_errors": 2,
                "rx_dropped": 10,
                "tx_dropped": 0,
            }
        ],
    }
    await client.put("/api/interfaces", json=payload)

    response = await client.get("/api/interfaces", params={"node_id": "r3"})
    iface = response.json()["interfaces"][0]
    assert iface["rx_errors"] == 5
    assert iface["tx_errors"] == 2
    assert iface["rx_dropped"] == 10


@pytest.mark.asyncio
async def test_get_all_interfaces(client: AsyncClient):
    """GET /api/interfaces/all returns all nodes with interfaces."""
    await create_node(client, "n1")
    await create_node(client, "n2")

    await client.put("/api/interfaces", json={
        "node_id": "n1",
        "interfaces": [{"name": "eth0", "state": "up", "rx_bps": 100.0}],
    })
    await client.put("/api/interfaces", json={
        "node_id": "n2",
        "interfaces": [{"name": "eth0", "state": "up", "rx_bps": 200.0}],
    })

    response = await client.get("/api/interfaces/all")
    assert response.status_code == 200
    data = response.json()
    node_ids = {entry["node_id"] for entry in data}
    assert "n1" in node_ids
    assert "n2" in node_ids


def test_interface_metrics_model():
    """Test InterfaceMetrics Pydantic model."""
    m = InterfaceMetrics(
        name="ethernet-1/1",
        state=InterfaceState.UP,
        rx_bps=5000000.0,
        tx_bps=1200000.0,
        rx_bytes_total=500000000,
        tx_bytes_total=120000000,
    )
    assert m.name == "ethernet-1/1"
    assert m.state == InterfaceState.UP
    assert m.rx_bps == 5000000.0
    assert m.rx_pps == 0.0  # default

    d = m.model_dump()
    m2 = InterfaceMetrics(**d)
    assert m2.rx_bytes_total == 500000000
