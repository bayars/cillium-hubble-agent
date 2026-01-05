#!/usr/bin/env python3
"""
API endpoint tests for lab deployment.

Tests the REST API endpoints:
- POST /api/labs - Deploy lab
- GET /api/labs - List labs
- GET /api/labs/{name} - Get lab
- GET /api/labs/{name}/topology - Get lab topology
- GET /api/links - Get all links
- DELETE /api/labs/{name} - Delete lab
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from httpx import AsyncClient, ASGITransport
from api.main import app
from api.services.link_state_service import reset_link_state_service


def load_yaml_file(path: str) -> str:
    """Load YAML file content."""
    with open(path, 'r') as f:
        return f.read()


@pytest.fixture(autouse=True)
def reset_service():
    """Reset link state service before each test."""
    reset_link_state_service()
    yield


@pytest.fixture
async def client():
    """Create async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_deploy_single_lab(client: AsyncClient):
    """Test deploying a single lab."""
    print("\n" + "=" * 60)
    print("TEST: Deploy Single Lab via API")
    print("=" * 60)

    yaml_content = load_yaml_file("samples/lab-dc1.yaml")

    response = await client.post(
        "/api/labs",
        json={
            "name": "dc1",
            "namespace": "clab",
            "containerlab_yaml": yaml_content,
        },
    )

    print(f"\nResponse status: {response.status_code}")
    print(f"Response body: {response.json()}")

    assert response.status_code == 200
    data = response.json()
    assert data["lab"] == "dc1"
    assert data["nodes_discovered"] == 5
    assert data["links_discovered"] == 5

    # Verify node IDs are prefixed
    for node_id in data["node_ids"]:
        assert node_id.startswith("dc1/"), f"Node ID not prefixed: {node_id}"

    # Verify link IDs are prefixed
    for link_id in data["link_ids"]:
        assert link_id.startswith("dc1/"), f"Link ID not prefixed: {link_id}"

    print("\n✓ Deploy single lab test PASSED")


@pytest.mark.asyncio
async def test_lab_exists_in_service(client: AsyncClient):
    """Test lab exists in service after deploy."""
    print("\n" + "=" * 60)
    print("TEST: Lab Exists in Service")
    print("=" * 60)

    # Deploy a lab
    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": yaml_content},
    )

    # Check lab exists in link state service
    from api.services.link_state_service import get_link_state_service
    service = get_link_state_service()
    labs = await service.get_labs()

    print(f"\nLabs in service: {labs}")
    assert "dc1" in labs, "dc1 should be in labs"

    # Check we can get the topology
    dc1_topo = await service.get_topology_by_lab("dc1")
    print(f"DC1 nodes: {dc1_topo['node_count']}")
    print(f"DC1 links: {dc1_topo['link_count']}")

    assert dc1_topo["node_count"] == 5
    assert dc1_topo["link_count"] == 5

    print("\n✓ Lab exists in service test PASSED")


@pytest.mark.asyncio
async def test_get_lab_topology(client: AsyncClient):
    """Test getting lab topology."""
    print("\n" + "=" * 60)
    print("TEST: Get Lab Topology via API")
    print("=" * 60)

    # Deploy a lab first
    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": yaml_content},
    )

    # Get topology
    response = await client.get("/api/labs/dc1/topology")

    print(f"\nResponse status: {response.status_code}")
    data = response.json()
    print(f"Lab: {data['lab']}")
    print(f"Nodes: {data['node_count']}")
    print(f"Links: {data['link_count']}")

    assert response.status_code == 200
    assert data["lab"] == "dc1"
    assert data["node_count"] == 5
    assert data["link_count"] == 5

    print("\n✓ Get lab topology test PASSED")


@pytest.mark.asyncio
async def test_deploy_dual_labs_unique_links(client: AsyncClient):
    """Test deploying two labs with unique links."""
    print("\n" + "=" * 60)
    print("TEST: Deploy Dual Labs - Unique Links via API")
    print("=" * 60)

    dc1_yaml = load_yaml_file("samples/lab-dc1.yaml")
    dc2_yaml = load_yaml_file("samples/lab-dc2.yaml")

    # Deploy DC1
    response1 = await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": dc1_yaml},
    )
    assert response1.status_code == 200
    print(f"\nDC1 deployed: {response1.json()['nodes_discovered']} nodes, {response1.json()['links_discovered']} links")

    # Deploy DC2
    response2 = await client.post(
        "/api/labs",
        json={"name": "dc2", "namespace": "clab", "containerlab_yaml": dc2_yaml},
    )
    assert response2.status_code == 200
    print(f"DC2 deployed: {response2.json()['nodes_discovered']} nodes, {response2.json()['links_discovered']} links")

    # Get all links
    response = await client.get("/api/links")
    assert response.status_code == 200
    data = response.json()

    print(f"\nTotal links: {data['count']}")

    # Check uniqueness
    link_ids = [link["id"] for link in data["links"]]
    unique_ids = set(link_ids)

    print(f"\nAll link IDs:")
    for lid in sorted(link_ids):
        print(f"  - {lid}")

    assert len(unique_ids) == len(link_ids), \
        f"Duplicate links found! Unique: {len(unique_ids)}, Total: {len(link_ids)}"

    # Verify both labs have links
    dc1_links = [lid for lid in link_ids if lid.startswith("dc1/")]
    dc2_links = [lid for lid in link_ids if lid.startswith("dc2/")]

    print(f"\nDC1 links: {len(dc1_links)}")
    print(f"DC2 links: {len(dc2_links)}")

    assert len(dc1_links) == 5
    assert len(dc2_links) == 6

    print("\n✓ Dual labs unique links test PASSED")


@pytest.mark.asyncio
async def test_traffic_generator_in_links(client: AsyncClient):
    """Test traffic generator nodes appear correctly in links."""
    print("\n" + "=" * 60)
    print("TEST: Traffic Generator in Links via API")
    print("=" * 60)

    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": yaml_content},
    )

    # Get all links
    response = await client.get("/api/links")
    assert response.status_code == 200
    data = response.json()

    # Find traffic generator links
    tgen_links = [
        link for link in data["links"]
        if "tgen" in link["source"] or "tgen" in link["target"]
    ]

    print(f"\nTraffic generator links found: {len(tgen_links)}")
    for link in tgen_links:
        print(f"\n  Link: {link['id']}")
        print(f"    Source: {link['source']} ({link['source_interface']})")
        print(f"    Target: {link['target']} ({link['target_interface']})")
        print(f"    Lab: {link['lab']}")

    assert len(tgen_links) == 2, f"Expected 2 tgen links, got {len(tgen_links)}"

    # Verify the links connect to tgen nodes
    for link in tgen_links:
        has_tgen = "tgen" in link["source"] or "tgen" in link["target"]
        assert has_tgen, f"Link {link['id']} should have tgen node"

    print("\n✓ Traffic generator in links test PASSED")


@pytest.mark.asyncio
async def test_clear_lab_links(client: AsyncClient):
    """Test clearing lab links (service-level, no K8s)."""
    print("\n" + "=" * 60)
    print("TEST: Clear Lab Links via Service")
    print("=" * 60)

    dc1_yaml = load_yaml_file("samples/lab-dc1.yaml")
    dc2_yaml = load_yaml_file("samples/lab-dc2.yaml")

    # Deploy both labs
    await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": dc1_yaml},
    )
    await client.post(
        "/api/labs",
        json={"name": "dc2", "namespace": "clab", "containerlab_yaml": dc2_yaml},
    )

    # Get links before clear
    response = await client.get("/api/links")
    links_before = response.json()["count"]
    print(f"\nLinks before clear: {links_before}")
    assert links_before == 11

    # Clear DC1 via service directly (avoid K8s API)
    from api.services.link_state_service import get_link_state_service
    service = get_link_state_service()
    await service.clear_lab("dc1")

    # Get links after clear
    response = await client.get("/api/links")
    links_after = response.json()["count"]
    print(f"Links after clear dc1: {links_after}")

    assert links_after == 6, f"Expected 6 links (DC2 only), got {links_after}"

    # Verify no DC1 links remain
    data = response.json()
    for link in data["links"]:
        assert not link["id"].startswith("dc1/"), f"DC1 link still present: {link['id']}"
        assert link["id"].startswith("dc2/"), f"Unexpected link: {link['id']}"

    print("\n✓ Clear lab links test PASSED")


@pytest.mark.asyncio
async def test_get_topology_with_labs(client: AsyncClient):
    """Test that /api/topology returns nodes with lab field."""
    print("\n" + "=" * 60)
    print("TEST: Topology Endpoint with Lab Field")
    print("=" * 60)

    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    await client.post(
        "/api/labs",
        json={"name": "dc1", "namespace": "clab", "containerlab_yaml": yaml_content},
    )

    # Get full topology
    response = await client.get("/api/topology")
    assert response.status_code == 200
    data = response.json()

    print(f"\nTopology nodes: {len(data['nodes'])}")
    print(f"Topology edges: {len(data['edges'])}")

    # Verify nodes have lab field
    for node in data["nodes"]:
        print(f"  Node: {node['id']} (lab={node.get('lab', 'MISSING')})")
        assert "lab" in node, f"Node missing lab field: {node['id']}"

    # Verify edges have lab field
    for edge in data["edges"]:
        print(f"  Edge: {edge['id']} (lab={edge.get('lab', 'MISSING')})")
        assert "lab" in edge, f"Edge missing lab field: {edge['id']}"

    print("\n✓ Topology with lab field test PASSED")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
