import pytest
from httpx import AsyncClient

from .helpers import create_node, create_link


@pytest.mark.asyncio
async def test_get_empty_topology(client: AsyncClient):
    """Test getting topology when empty."""
    response = await client.get("/api/topology")
    assert response.status_code == 200
    data = response.json()
    assert data["nodes"] == []
    assert data["edges"] == []


@pytest.mark.asyncio
async def test_add_node(client: AsyncClient):
    """Test adding a node."""
    response = await create_node(client, "router1", "R1", status="up", platform="srlinux")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "router1"
    assert data["label"] == "R1"


@pytest.mark.asyncio
async def test_add_link(client: AsyncClient):
    """Test adding a link."""
    await create_node(client, "router1", "R1")
    await create_node(client, "router2", "R2")

    response = await create_link(
        client, "link1", "router1", "router2", "eth1", "eth1", "active", speed_mbps=10000
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "link1"
    assert data["state"] == "active"


@pytest.mark.asyncio
async def test_topology_contains_added_items(client: AsyncClient):
    """Test topology returns added nodes and links."""
    await create_node(client, "r1")
    await create_node(client, "r2")
    await create_link(client, "link1")

    response = await client.get("/api/topology")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_remove_node(client: AsyncClient):
    """Test removing a node."""
    await create_node(client, "r1")

    response = await client.delete("/api/topology/nodes/r1")
    assert response.status_code == 200

    topo = await client.get("/api/topology")
    assert len(topo.json()["nodes"]) == 0


@pytest.mark.asyncio
async def test_remove_link(client: AsyncClient):
    """Test removing a link."""
    await create_node(client, "r1")
    await create_node(client, "r2")
    await create_link(client, "link1")

    response = await client.delete("/api/topology/links/link1")
    assert response.status_code == 200

    topo = await client.get("/api/topology")
    assert len(topo.json()["edges"]) == 0
