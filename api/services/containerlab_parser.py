"""
Containerlab YAML Parser.

Parses containerlab topology definitions to extract nodes and links
for visualization in the Network Monitor.
"""

import logging
from typing import Optional

import yaml

from ..models.schemas import Node, Link, NodeStatus, LinkState

logger = logging.getLogger(__name__)


class ContainerlabParser:
    """Parse containerlab YAML to extract nodes and links."""

    # Mapping of containerlab kinds to node types
    KIND_TO_TYPE = {
        "srl": "router",
        "nokia_srlinux": "router",
        "ceos": "router",
        "arista_ceos": "router",
        "vr-sros": "router",
        "crpd": "router",
        "frr": "router",
        "linux": "host",
        "bridge": "switch",
        "ovs-bridge": "switch",
    }

    @staticmethod
    def parse(
        yaml_content: str,
        lab_name: str,
    ) -> tuple[list[Node], list[Link]]:
        """
        Parse containerlab topology definition.

        Args:
            yaml_content: Containerlab YAML content
            lab_name: Lab name for prefixing IDs

        Returns:
            Tuple of (nodes, links) with lab-prefixed IDs

        Raises:
            ValueError: If YAML is invalid or missing required fields
        """
        try:
            data = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML: {e}")

        if not isinstance(data, dict):
            raise ValueError("Invalid containerlab YAML: root must be a dictionary")

        # Get topology section
        topology = data.get("topology", {})
        if not topology:
            raise ValueError("Missing 'topology' section in containerlab YAML")

        nodes = ContainerlabParser._parse_nodes(topology, lab_name)
        links = ContainerlabParser._parse_links(topology, lab_name)

        return nodes, links

    @staticmethod
    def _parse_nodes(topology: dict, lab_name: str) -> list[Node]:
        """Parse nodes from topology section."""
        nodes = []
        nodes_section = topology.get("nodes", {})

        # Get kind defaults
        kinds_section = topology.get("kinds", {})

        for node_name, node_config in nodes_section.items():
            node_config = node_config or {}

            # Determine node kind (check node-level, then kinds defaults)
            kind = node_config.get("kind", "linux")

            # Get node type from kind
            node_type = ContainerlabParser.KIND_TO_TYPE.get(kind, "host")

            # Get image to determine platform
            image = node_config.get("image", "")
            platform = ContainerlabParser._detect_platform(kind, image)

            # Create prefixed node ID
            node_id = f"{lab_name}/{node_name}"

            node = Node(
                id=node_id,
                lab=lab_name,
                label=node_name,
                type=node_type,
                status=NodeStatus.UNKNOWN,
                platform=platform,
                metadata={
                    "kind": kind,
                    "image": image,
                    "original_name": node_name,
                },
            )
            nodes.append(node)

        logger.info(f"Parsed {len(nodes)} nodes from containerlab topology")
        return nodes

    @staticmethod
    def _parse_links(topology: dict, lab_name: str) -> list[Link]:
        """Parse links from topology section."""
        links = []
        links_section = topology.get("links", [])

        for idx, link_config in enumerate(links_section):
            endpoints = link_config.get("endpoints", [])

            if len(endpoints) != 2:
                logger.warning(f"Skipping invalid link {idx}: expected 2 endpoints")
                continue

            try:
                source_node, source_iface = ContainerlabParser._parse_endpoint(
                    endpoints[0]
                )
                target_node, target_iface = ContainerlabParser._parse_endpoint(
                    endpoints[1]
                )
            except ValueError as e:
                logger.warning(f"Skipping invalid link {idx}: {e}")
                continue

            # Create prefixed IDs
            source_id = f"{lab_name}/{source_node}"
            target_id = f"{lab_name}/{target_node}"
            link_id = f"{lab_name}/{source_node}-{target_node}"

            link = Link(
                id=link_id,
                lab=lab_name,
                source=source_id,
                target=target_id,
                source_interface=source_iface,
                target_interface=target_iface,
                state=LinkState.UNKNOWN,
                metadata={
                    "original_endpoints": endpoints,
                },
            )
            links.append(link)

        logger.info(f"Parsed {len(links)} links from containerlab topology")
        return links

    @staticmethod
    def _parse_endpoint(endpoint: str) -> tuple[str, str]:
        """
        Parse endpoint string in format 'node:interface'.

        Args:
            endpoint: Endpoint string (e.g., "spine1:e1-1")

        Returns:
            Tuple of (node_name, interface_name)

        Raises:
            ValueError: If endpoint format is invalid
        """
        if ":" not in endpoint:
            raise ValueError(f"Invalid endpoint format: {endpoint}")

        parts = endpoint.split(":", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid endpoint format: {endpoint}")

        return parts[0], parts[1]

    @staticmethod
    def _detect_platform(kind: str, image: str) -> Optional[str]:
        """Detect platform from kind and image."""
        if kind in ("srl", "nokia_srlinux") or "srlinux" in image.lower():
            return "srlinux"
        if kind in ("ceos", "arista_ceos") or "ceos" in image.lower():
            return "ceos"
        if kind == "frr" or "frr" in image.lower():
            return "frr"
        if "iperf" in image.lower():
            return "iperf"
        return kind

    @staticmethod
    def parse_clabernetes_crd(yaml_content: str) -> tuple[str, str, str]:
        """
        Parse Clabernetes Topology CRD to extract containerlab definition.

        Args:
            yaml_content: Full Clabernetes CRD YAML

        Returns:
            Tuple of (name, namespace, containerlab_yaml)
        """
        try:
            data = yaml.safe_load(yaml_content)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML: {e}")

        # Extract metadata
        metadata = data.get("metadata", {})
        name = metadata.get("name", "")
        namespace = metadata.get("namespace", "clab")

        # Extract containerlab definition
        spec = data.get("spec", {})
        definition = spec.get("definition", {})
        containerlab_yaml = definition.get("containerlab", "")

        if not containerlab_yaml:
            raise ValueError("No containerlab definition found in CRD")

        return name, namespace, containerlab_yaml
