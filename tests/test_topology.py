import pytest
from httpx import AsyncClient


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
    node = {
        "id": "router1",
        "label": "R1",
        "type": "router",
        "status": "up",
        "platform": "srlinux",
    }
    response = await client.post("/api/topology/nodes", json=node)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "router1"
    assert data["label"] == "R1"


@pytest.mark.asyncio
async def test_add_link(client: AsyncClient):
    """Test adding a link."""
    # Add nodes first
    await client.post("/api/topology/nodes", json={
        "id": "router1", "label": "R1", "type": "router",
    })
    await client.post("/api/topology/nodes", json={
        "id": "router2", "label": "R2", "type": "router",
    })

    link = {
        "id": "link1",
        "source": "router1",
        "target": "router2",
        "source_interface": "eth1",
        "target_interface": "eth1",
        "state": "active",
        "speed_mbps": 10000,
    }
    response = await client.post("/api/topology/links", json=link)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "link1"
    assert data["state"] == "active"


@pytest.mark.asyncio
async def test_topology_contains_added_items(client: AsyncClient):
    """Test topology returns added nodes and links."""
    await client.post("/api/topology/nodes", json={
        "id": "r1", "label": "R1", "type": "router",
    })
    await client.post("/api/topology/nodes", json={
        "id": "r2", "label": "R2", "type": "router",
    })
    await client.post("/api/topology/links", json={
        "id": "link1",
        "source": "r1",
        "target": "r2",
        "source_interface": "eth0",
        "target_interface": "eth0",
    })

    response = await client.get("/api/topology")
    assert response.status_code == 200
    data = response.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1


@pytest.mark.asyncio
async def test_remove_node(client: AsyncClient):
    """Test removing a node."""
    await client.post("/api/topology/nodes", json={
        "id": "r1", "label": "R1", "type": "router",
    })

    response = await client.delete("/api/topology/nodes/r1")
    assert response.status_code == 200

    topo = await client.get("/api/topology")
    assert len(topo.json()["nodes"]) == 0


@pytest.mark.asyncio
async def test_remove_link(client: AsyncClient):
    """Test removing a link."""
    await client.post("/api/topology/nodes", json={
        "id": "r1", "label": "R1", "type": "router",
    })
    await client.post("/api/topology/nodes", json={
        "id": "r2", "label": "R2", "type": "router",
    })
    await client.post("/api/topology/links", json={
        "id": "link1",
        "source": "r1",
        "target": "r2",
        "source_interface": "eth0",
        "target_interface": "eth0",
    })

    response = await client.delete("/api/topology/links/link1")
    assert response.status_code == 200

    topo = await client.get("/api/topology")
    assert len(topo.json()["edges"]) == 0
