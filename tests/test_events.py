import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_post_event(client: AsyncClient):
    """Test posting an event."""
    # Add topology first
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
            "state": "idle",
        },
    )

    event = {
        "interface": "eth0",
        "ifindex": 2,
        "old_state": "idle",
        "new_state": "active",
        "operstate": "up",
        "source": "agent",
    }
    response = await client.post("/api/events", json=event)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_event_history(client: AsyncClient):
    """Test getting event history."""
    response = await client.get("/api/events/history")
    assert response.status_code == 200
    data = response.json()
    assert "events" in data
