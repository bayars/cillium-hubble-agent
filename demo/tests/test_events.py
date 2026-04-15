import pytest
from httpx import AsyncClient

from .helpers import create_node, create_link


@pytest.mark.asyncio
async def test_post_event(client: AsyncClient):
    """Test posting an event."""
    await create_node(client, "r1")
    await create_node(client, "r2")
    await create_link(client, "link1")

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
