import pytest

from api.models.schemas import Node, Link, LinkState, LinkMetrics, NodeStatus
from api.services.link_state_service import get_link_state_service


@pytest.mark.asyncio
async def test_initialize_topology():
    """Test initializing topology."""
    service = get_link_state_service()
    nodes = [
        Node(id="r1", label="R1", type="router", status=NodeStatus.UP),
        Node(id="r2", label="R2", type="router", status=NodeStatus.UP),
    ]
    links = [
        Link(id="l1", source="r1", target="r2",
             source_interface="eth0", target_interface="eth0",
             state=LinkState.ACTIVE),
    ]
    await service.initialize_topology(nodes, links)

    topo = await service.get_topology()
    assert len(topo.nodes) == 2
    assert len(topo.edges) == 1


@pytest.mark.asyncio
async def test_update_link_state():
    """Test link state transitions generate events."""
    service = get_link_state_service()
    nodes = [
        Node(id="r1", label="R1", type="router"),
        Node(id="r2", label="R2", type="router"),
    ]
    links = [
        Link(id="l1", source="r1", target="r2",
             source_interface="eth0", target_interface="eth0",
             state=LinkState.ACTIVE),
    ]
    await service.initialize_topology(nodes, links)

    event = await service.update_link_state("l1", LinkState.DOWN)
    assert event is not None
    assert event.old_state == LinkState.ACTIVE
    assert event.new_state == LinkState.DOWN


@pytest.mark.asyncio
async def test_no_event_on_same_state():
    """Test no event when state doesn't change."""
    service = get_link_state_service()
    nodes = [Node(id="r1", label="R1", type="router")]
    links = [
        Link(id="l1", source="r1", target="r1",
             source_interface="eth0", target_interface="eth1",
             state=LinkState.ACTIVE),
    ]
    await service.initialize_topology(nodes, links)

    event = await service.update_link_state("l1", LinkState.ACTIVE)
    assert event is None


@pytest.mark.asyncio
async def test_update_link_metrics():
    """Test updating link metrics."""
    service = get_link_state_service()
    nodes = [Node(id="r1", label="R1", type="router")]
    links = [
        Link(id="l1", source="r1", target="r1",
             source_interface="eth0", target_interface="eth1"),
    ]
    await service.initialize_topology(nodes, links)

    metrics = LinkMetrics(rx_bps=1000000, tx_bps=500000, utilization=0.1)
    await service.update_link_metrics("l1", metrics)

    link = await service.get_link("l1")
    assert link.metrics.rx_bps == 1000000
    assert link.metrics.tx_bps == 500000


@pytest.mark.asyncio
async def test_get_link_by_interface():
    """Test finding a link by interface name."""
    service = get_link_state_service()
    nodes = [Node(id="r1", label="R1", type="router")]
    links = [
        Link(id="l1", source="r1", target="r1",
             source_interface="eth0", target_interface="eth1"),
    ]
    await service.initialize_topology(nodes, links)

    link = await service.get_link_by_interface("eth0")
    assert link is not None
    assert link.id == "l1"

    link = await service.get_link_by_interface("eth1")
    assert link is not None
    assert link.id == "l1"

    link = await service.get_link_by_interface("nonexistent")
    assert link is None


@pytest.mark.asyncio
async def test_lab_isolation():
    """Test labs are isolated from each other."""
    service = get_link_state_service()

    # Add nodes/links for two labs
    await service.add_node(Node(id="dc1/r1", label="R1", type="router", lab="dc1"))
    await service.add_node(Node(id="dc2/r1", label="R1", type="router", lab="dc2"))
    await service.add_link(Link(
        id="dc1/l1", source="dc1/r1", target="dc1/r1",
        source_interface="dc1-eth0", target_interface="dc1-eth1", lab="dc1",
    ))
    await service.add_link(Link(
        id="dc2/l1", source="dc2/r1", target="dc2/r1",
        source_interface="dc2-eth0", target_interface="dc2-eth1", lab="dc2",
    ))

    dc1 = await service.get_topology_by_lab("dc1")
    dc2 = await service.get_topology_by_lab("dc2")

    assert dc1["node_count"] == 1
    assert dc1["link_count"] == 1
    assert dc2["node_count"] == 1
    assert dc2["link_count"] == 1


@pytest.mark.asyncio
async def test_clear_lab():
    """Test clearing a lab removes only its resources."""
    service = get_link_state_service()

    await service.add_node(Node(id="dc1/r1", label="R1", type="router", lab="dc1"))
    await service.add_node(Node(id="dc2/r1", label="R1", type="router", lab="dc2"))
    await service.add_link(Link(
        id="dc1/l1", source="dc1/r1", target="dc1/r1",
        source_interface="dc1-eth0", target_interface="dc1-eth1", lab="dc1",
    ))
    await service.add_link(Link(
        id="dc2/l1", source="dc2/r1", target="dc2/r1",
        source_interface="dc2-eth0", target_interface="dc2-eth1", lab="dc2",
    ))

    await service.clear_lab("dc1")

    all_links = await service.get_all_links()
    assert len(all_links) == 1
    assert all_links[0].id == "dc2/l1"

    labs = await service.get_labs()
    assert "dc1" not in labs
    assert "dc2" in labs


@pytest.mark.asyncio
async def test_get_stats():
    """Test service stats."""
    service = get_link_state_service()
    nodes = [Node(id="r1", label="R1", type="router")]
    links = [
        Link(id="l1", source="r1", target="r1",
             source_interface="eth0", target_interface="eth1",
             state=LinkState.ACTIVE),
        Link(id="l2", source="r1", target="r1",
             source_interface="eth2", target_interface="eth3",
             state=LinkState.DOWN),
    ]
    await service.initialize_topology(nodes, links)

    stats = service.get_stats()
    assert stats["node_count"] == 1
    assert stats["link_count"] == 2
    assert stats["link_states"]["active"] == 1
    assert stats["link_states"]["down"] == 1
