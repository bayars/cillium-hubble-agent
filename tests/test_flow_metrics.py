"""
Tests for flow-based metrics and data_source attribution.

Validates that:
- LinkMetrics supports flow-based fields (flow_count, flows_per_second, etc.)
- data_source field is properly stored and returned
- Hubble vs iperf3 metrics are correctly represented
"""

import pytest
from httpx import AsyncClient

from api.models.schemas import LinkMetrics


async def _create_link(client: AsyncClient, link_id: str = "link1"):
    """Helper: create nodes and a link."""
    await client.post(
        "/api/topology/nodes",
        json={"id": "r1", "label": "R1", "type": "router"},
    )
    await client.post(
        "/api/topology/nodes",
        json={"id": "r2", "label": "R2", "type": "router"},
    )
    await client.post(
        "/api/topology/links",
        json={
            "id": link_id,
            "source": "r1",
            "target": "r2",
            "source_interface": "eth0",
            "target_interface": "eth0",
            "state": "idle",
        },
    )


@pytest.mark.asyncio
async def test_link_metrics_default_data_source(client: AsyncClient):
    """New links should have data_source='none' by default."""
    await _create_link(client)

    response = await client.get("/api/links/link1")
    assert response.status_code == 200
    data = response.json()
    assert data["metrics"]["data_source"] == "none"
    assert data["metrics"]["flow_count"] == 0
    assert data["metrics"]["flows_per_second"] == 0.0


@pytest.mark.asyncio
async def test_push_hubble_flow_metrics(client: AsyncClient):
    """Push Hubble-style flow metrics (no bandwidth)."""
    await _create_link(client)

    metrics = {
        "flow_count": 1523,
        "flows_per_second": 42.5,
        "flows_forwarded": 1500,
        "flows_dropped": 23,
        "active_connections": 8,
        "protocols": {"TCP": 1200, "UDP": 323},
        "data_source": "hubble",
        # Hubble does NOT provide bandwidth
        "rx_bps": 0,
        "tx_bps": 0,
        "utilization": 0,
    }
    response = await client.put("/api/links/link1/metrics", json=metrics)
    assert response.status_code == 200
    data = response.json()

    assert data["metrics"]["data_source"] == "hubble"
    assert data["metrics"]["flow_count"] == 1523
    assert data["metrics"]["flows_per_second"] == 42.5
    assert data["metrics"]["flows_forwarded"] == 1500
    assert data["metrics"]["flows_dropped"] == 23
    assert data["metrics"]["protocols"] == {"TCP": 1200, "UDP": 323}
    # Bandwidth should be zero from Hubble
    assert data["metrics"]["rx_bps"] == 0
    assert data["metrics"]["tx_bps"] == 0


@pytest.mark.asyncio
async def test_push_iperf3_metrics(client: AsyncClient):
    """Push real iperf3 measured bandwidth."""
    await _create_link(client)

    metrics = {
        "rx_bps": 12500000,
        "tx_bps": 625000,
        "utilization": 0.1,
        "data_source": "iperf3",
    }
    response = await client.put("/api/links/link1/metrics", json=metrics)
    assert response.status_code == 200
    data = response.json()

    assert data["metrics"]["data_source"] == "iperf3"
    assert data["metrics"]["rx_bps"] == 12500000
    assert data["metrics"]["tx_bps"] == 625000
    assert data["metrics"]["utilization"] == 0.1


@pytest.mark.asyncio
async def test_metrics_data_source_persists(client: AsyncClient):
    """Verify data_source persists across reads."""
    await _create_link(client)

    await client.put(
        "/api/links/link1/metrics",
        json={"data_source": "hubble", "flow_count": 100},
    )

    # GET should return the same data_source
    response = await client.get("/api/links/link1")
    assert response.json()["metrics"]["data_source"] == "hubble"

    # GET /metrics should also return it
    response = await client.get("/api/links/link1/metrics")
    assert response.json()["data_source"] == "hubble"


@pytest.mark.asyncio
async def test_links_list_includes_flow_metrics(client: AsyncClient):
    """GET /api/links returns flow metrics in the response."""
    await _create_link(client)

    await client.put(
        "/api/links/link1/metrics",
        json={
            "flow_count": 500,
            "flows_per_second": 10.0,
            "data_source": "hubble",
        },
    )

    response = await client.get("/api/links")
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    link = data["links"][0]
    assert link["metrics"]["flow_count"] == 500
    assert link["metrics"]["data_source"] == "hubble"


def test_link_metrics_model_validation():
    """Test LinkMetrics Pydantic model with flow fields."""
    m = LinkMetrics(
        flow_count=100,
        flows_per_second=5.0,
        flows_forwarded=95,
        flows_dropped=5,
        active_connections=3,
        protocols={"TCP": 80, "UDP": 20},
        data_source="hubble",
    )
    assert m.flow_count == 100
    assert m.data_source == "hubble"
    assert m.rx_bps == 0.0  # Not set, default
    assert m.protocols == {"TCP": 80, "UDP": 20}

    # Serialize and deserialize
    d = m.model_dump()
    assert d["data_source"] == "hubble"
    m2 = LinkMetrics(**d)
    assert m2.flow_count == 100
