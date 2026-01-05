#!/usr/bin/env python3
"""
Test script for lab deployment and link creation.

Tests:
1. Single lab deployment
2. Link creation correctness
3. Traffic generator node info
4. Dual lab deployment with unique links
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.services.containerlab_parser import ContainerlabParser
from api.services.link_state_service import get_link_state_service, reset_link_state_service
from api.models.schemas import LabDeployRequest, Node, Link


def load_yaml_file(path: str) -> str:
    """Load YAML file content."""
    with open(path, 'r') as f:
        return f.read()


async def test_containerlab_parser():
    """Test containerlab YAML parsing."""
    print("\n" + "=" * 60)
    print("TEST: Containerlab Parser")
    print("=" * 60)

    # Load DC1 topology
    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    nodes, links = ContainerlabParser.parse(yaml_content, "dc1")

    print(f"\nParsed DC1 topology:")
    print(f"  Nodes: {len(nodes)}")
    for node in nodes:
        print(f"    - {node.id} (label={node.label}, type={node.type}, platform={node.platform})")

    print(f"\n  Links: {len(links)}")
    for link in links:
        print(f"    - {link.id}: {link.source}:{link.source_interface} <-> {link.target}:{link.target_interface}")

    # Verify traffic generators are parsed correctly
    tgen_nodes = [n for n in nodes if "tgen" in n.id]
    print(f"\n  Traffic generators found: {len(tgen_nodes)}")
    for tgen in tgen_nodes:
        print(f"    - {tgen.id}: platform={tgen.platform}, metadata={tgen.metadata}")

    assert len(nodes) == 5, f"Expected 5 nodes, got {len(nodes)}"
    assert len(links) == 5, f"Expected 5 links, got {len(links)}"
    assert len(tgen_nodes) == 2, f"Expected 2 traffic generators, got {len(tgen_nodes)}"

    # Verify all IDs are prefixed
    for node in nodes:
        assert node.id.startswith("dc1/"), f"Node ID not prefixed: {node.id}"
        assert node.lab == "dc1", f"Node lab not set: {node.lab}"

    for link in links:
        assert link.id.startswith("dc1/"), f"Link ID not prefixed: {link.id}"
        assert link.source.startswith("dc1/"), f"Link source not prefixed: {link.source}"
        assert link.target.startswith("dc1/"), f"Link target not prefixed: {link.target}"
        assert link.lab == "dc1", f"Link lab not set: {link.lab}"

    print("\n✓ Containerlab parser test PASSED")
    return nodes, links


async def test_link_state_service():
    """Test link state service with lab support."""
    print("\n" + "=" * 60)
    print("TEST: Link State Service - Single Lab")
    print("=" * 60)

    # Reset service
    reset_link_state_service()
    service = get_link_state_service()

    # Parse and add DC1 topology
    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    nodes, links = ContainerlabParser.parse(yaml_content, "dc1")

    # Add nodes and links
    for node in nodes:
        await service.add_node(node)
    for link in links:
        await service.add_link(link)

    # Verify labs are tracked
    labs = await service.get_labs()
    print(f"\nRegistered labs: {labs}")
    assert "dc1" in labs, "dc1 not in labs"

    # Get topology by lab
    dc1_topo = await service.get_topology_by_lab("dc1")
    print(f"\nDC1 topology:")
    print(f"  Nodes: {dc1_topo['node_count']}")
    print(f"  Links: {dc1_topo['link_count']}")

    assert dc1_topo['node_count'] == 5
    assert dc1_topo['link_count'] == 5

    # Get all links
    all_links = await service.get_all_links()
    print(f"\nAll links in service: {len(all_links)}")
    for link in all_links:
        print(f"  - {link.id} (lab={link.lab})")

    print("\n✓ Link state service single lab test PASSED")


async def test_dual_lab_deployment():
    """Test deploying two labs with unique links."""
    print("\n" + "=" * 60)
    print("TEST: Dual Lab Deployment - Unique Links")
    print("=" * 60)

    # Reset service
    reset_link_state_service()
    service = get_link_state_service()

    # Parse both topologies
    dc1_yaml = load_yaml_file("samples/lab-dc1.yaml")
    dc2_yaml = load_yaml_file("samples/lab-dc2.yaml")

    dc1_nodes, dc1_links = ContainerlabParser.parse(dc1_yaml, "dc1")
    dc2_nodes, dc2_links = ContainerlabParser.parse(dc2_yaml, "dc2")

    print(f"\nDC1: {len(dc1_nodes)} nodes, {len(dc1_links)} links")
    print(f"DC2: {len(dc2_nodes)} nodes, {len(dc2_links)} links")

    # Add all nodes and links from both labs
    for node in dc1_nodes + dc2_nodes:
        await service.add_node(node)
    for link in dc1_links + dc2_links:
        await service.add_link(link)

    # Get all links
    all_links = await service.get_all_links()
    all_link_ids = [link.id for link in all_links]

    print(f"\nTotal links in service: {len(all_links)}")
    print("\nAll link IDs:")
    for lid in sorted(all_link_ids):
        print(f"  - {lid}")

    # Verify uniqueness
    unique_ids = set(all_link_ids)
    assert len(unique_ids) == len(all_link_ids), \
        f"Duplicate link IDs found! Unique: {len(unique_ids)}, Total: {len(all_link_ids)}"

    # Verify both labs are tracked
    labs = await service.get_labs()
    print(f"\nRegistered labs: {labs}")
    assert "dc1" in labs and "dc2" in labs, "Not all labs registered"

    # Get topology by each lab
    dc1_topo = await service.get_topology_by_lab("dc1")
    dc2_topo = await service.get_topology_by_lab("dc2")

    print(f"\nDC1 topology: {dc1_topo['node_count']} nodes, {dc1_topo['link_count']} links")
    print(f"DC2 topology: {dc2_topo['node_count']} nodes, {dc2_topo['link_count']} links")

    # Verify isolation
    dc1_link_ids = [link.id for link in dc1_topo['links']]
    dc2_link_ids = [link.id for link in dc2_topo['links']]

    print(f"\nDC1 links: {dc1_link_ids}")
    print(f"DC2 links: {dc2_link_ids}")

    # No overlap between labs
    overlap = set(dc1_link_ids) & set(dc2_link_ids)
    assert len(overlap) == 0, f"Overlap found between labs: {overlap}"

    # All dc1 links start with dc1/
    for lid in dc1_link_ids:
        assert lid.startswith("dc1/"), f"DC1 link not prefixed: {lid}"

    # All dc2 links start with dc2/
    for lid in dc2_link_ids:
        assert lid.startswith("dc2/"), f"DC2 link not prefixed: {lid}"

    print("\n✓ Dual lab deployment test PASSED - All links are unique!")


async def test_traffic_generator_links():
    """Test that traffic generator links have correct info."""
    print("\n" + "=" * 60)
    print("TEST: Traffic Generator Link Info")
    print("=" * 60)

    # Reset service
    reset_link_state_service()
    service = get_link_state_service()

    # Parse DC1 topology
    yaml_content = load_yaml_file("samples/lab-dc1.yaml")
    nodes, links = ContainerlabParser.parse(yaml_content, "dc1")

    # Add nodes and links
    for node in nodes:
        await service.add_node(node)
    for link in links:
        await service.add_link(link)

    # Find traffic generator links
    tgen_links = [link for link in links if "tgen" in link.source or "tgen" in link.target]

    print(f"\nTraffic generator links: {len(tgen_links)}")
    for link in tgen_links:
        print(f"\n  Link: {link.id}")
        print(f"    Source: {link.source} ({link.source_interface})")
        print(f"    Target: {link.target} ({link.target_interface})")
        print(f"    State: {link.state}")
        print(f"    Lab: {link.lab}")
        print(f"    Metadata: {link.metadata}")

    # Verify we found the expected tgen links
    assert len(tgen_links) == 2, f"Expected 2 tgen links, got {len(tgen_links)}"

    # Verify tgen nodes have correct platform
    tgen_nodes = [n for n in nodes if "tgen" in n.id]
    for node in tgen_nodes:
        print(f"\n  Traffic Generator Node: {node.id}")
        print(f"    Platform: {node.platform}")
        print(f"    Kind: {node.metadata.get('kind')}")
        print(f"    Image: {node.metadata.get('image')}")
        assert node.platform == "iperf", f"Expected platform 'iperf', got {node.platform}"

    print("\n✓ Traffic generator link info test PASSED")


async def test_clear_lab():
    """Test clearing a lab."""
    print("\n" + "=" * 60)
    print("TEST: Clear Lab")
    print("=" * 60)

    # Reset service
    reset_link_state_service()
    service = get_link_state_service()

    # Add both labs
    dc1_yaml = load_yaml_file("samples/lab-dc1.yaml")
    dc2_yaml = load_yaml_file("samples/lab-dc2.yaml")

    dc1_nodes, dc1_links = ContainerlabParser.parse(dc1_yaml, "dc1")
    dc2_nodes, dc2_links = ContainerlabParser.parse(dc2_yaml, "dc2")

    for node in dc1_nodes + dc2_nodes:
        await service.add_node(node)
    for link in dc1_links + dc2_links:
        await service.add_link(link)

    # Verify both labs exist
    all_links_before = await service.get_all_links()
    print(f"\nBefore clear: {len(all_links_before)} total links")

    # Clear DC1
    await service.clear_lab("dc1")

    # Verify DC1 is gone
    all_links_after = await service.get_all_links()
    print(f"After clear dc1: {len(all_links_after)} total links")

    labs = await service.get_labs()
    print(f"Remaining labs: {labs}")

    assert "dc1" not in labs, "dc1 should be removed"
    assert "dc2" in labs, "dc2 should still exist"
    assert len(all_links_after) == len(dc2_links), \
        f"Expected {len(dc2_links)} links, got {len(all_links_after)}"

    # Verify only DC2 links remain
    for link in all_links_after:
        assert link.id.startswith("dc2/"), f"Non-dc2 link found: {link.id}"

    print("\n✓ Clear lab test PASSED")


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("NETWORK MONITOR - LAB DEPLOYMENT TESTS")
    print("=" * 60)

    try:
        await test_containerlab_parser()
        await test_link_state_service()
        await test_dual_lab_deployment()
        await test_traffic_generator_links()
        await test_clear_lab()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED!")
        print("=" * 60 + "\n")

    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
