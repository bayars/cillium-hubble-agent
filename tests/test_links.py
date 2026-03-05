import pytest
from httpx import AsyncClient


async def _seed_topology(client: AsyncClient):
    """Helper to create a basic topology."""
    await client.post(
        "/api/topology/nodes",
        json={
            "id": "r1",
            "label": "R1",
            "type": "router",
        },
    )
    await client.post(
        "/api/topology/nodes",
        json={
            "id": "r2",
            "label": "R2",
            "type": "router",
        },
    )
    await client.post(
        "/api/topology/links",
        json={
            "id": "link1",
            "source": "r1",
            "target": "r2",
            "source_interface": "eth0",
            "target_interface": "eth0",
            "state": "active",
            "speed_mbps": 1000,
        },
    )
    await client.post(
        "/api/topology/links",
        json={
            "id": "link2",
            "source": "r1",
            "target": "r2",
            "source_interface": "eth1",
            "target_interface": "eth1",
            "state": "idle",
            "speed_mbps": 10000,
        },
    )


@pytest.mark.asyncio
async def test_get_all_links(client: AsyncClient):
    """Test getting all links."""
    await _seed_topology(client)

    response = await client.get("/api/links")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 2


@pytest.mark.asyncio
async def test_filter_links_by_state(client: AsyncClient):
    """Test filtering links by state."""
    await _seed_topology(client)

    response = await client.get("/api/links?state=active")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["links"][0]["state"] == "active"


@pytest.mark.asyncio
async def test_get_single_link(client: AsyncClient):
    """Test getting a single link by ID."""
    await _seed_topology(client)

    response = await client.get("/api/links/link1")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "link1"
    assert data["source"] == "r1"
    assert data["target"] == "r2"


@pytest.mark.asyncio
async def test_get_nonexistent_link(client: AsyncClient):
    """Test 404 for nonexistent link."""
    response = await client.get("/api/links/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_link_state(client: AsyncClient):
    """Test updating link state."""
    await _seed_topology(client)

    response = await client.put("/api/links/link1/state?state=down")
    assert response.status_code == 200

    # Verify state changed
    link = await client.get("/api/links/link1")
    assert link.json()["state"] == "down"


@pytest.mark.asyncio
async def test_update_link_metrics(client: AsyncClient):
    """Test updating link metrics."""
    await _seed_topology(client)

    metrics = {
        "rx_bps": 100000000,
        "tx_bps": 50000000,
        "rx_pps": 82000,
        "tx_pps": 41000,
        "utilization": 0.1,
    }
    response = await client.put("/api/links/link1/metrics", json=metrics)
    assert response.status_code == 200
    data = response.json()
    assert data["metrics"]["rx_bps"] == 100000000
    assert data["metrics"]["tx_bps"] == 50000000
    assert data["metrics"]["utilization"] == 0.1


@pytest.mark.asyncio
async def test_update_metrics_nonexistent_link(client: AsyncClient):
    """Test 404 when updating metrics for nonexistent link."""
    metrics = {"rx_bps": 100, "tx_bps": 50, "utilization": 0.01}
    response = await client.put("/api/links/nonexistent/metrics", json=metrics)
    assert response.status_code == 404
