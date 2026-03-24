"""Shared test helpers for topology setup."""

from httpx import AsyncClient


async def create_node(client: AsyncClient, node_id: str = "r1", label: str | None = None, **kwargs):
    """Create a node via the API. Returns the response."""
    payload = {"id": node_id, "label": label or node_id.upper(), "type": "router", **kwargs}
    return await client.post("/api/topology/nodes", json=payload)


async def create_link(
    client: AsyncClient,
    link_id: str = "link1",
    source: str = "r1",
    target: str = "r2",
    source_interface: str = "eth0",
    target_interface: str = "eth0",
    state: str = "idle",
    **kwargs,
):
    """Create a link via the API. Returns the response."""
    payload = {
        "id": link_id,
        "source": source,
        "target": target,
        "source_interface": source_interface,
        "target_interface": target_interface,
        "state": state,
        **kwargs,
    }
    return await client.post("/api/topology/links", json=payload)


async def seed_topology(client: AsyncClient):
    """Create a basic two-node, two-link topology."""
    await create_node(client, "r1", "R1")
    await create_node(client, "r2", "R2")
    await create_link(client, "link1", "r1", "r2", "eth0", "eth0", "active", speed_mbps=1000)
    await create_link(client, "link2", "r1", "r2", "eth1", "eth1", "idle", speed_mbps=10000)
