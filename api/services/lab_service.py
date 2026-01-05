"""
Lab Service - Orchestrates lab lifecycle management.

Handles:
- Deploying labs (parsing containerlab, creating K8s CRDs)
- Importing topology for visualization
- Monitoring lab status
- Deleting labs
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from ..models.schemas import (
    Lab,
    LabStatus,
    LabDeployRequest,
    LabDeployResponse,
    Node,
    Link,
)
from .containerlab_parser import ContainerlabParser
from .k8s_client import get_k8s_manager, K8sLabManager
from .link_state_service import get_link_state_service

logger = logging.getLogger(__name__)


class LabService:
    """
    Manages lab lifecycle.

    Coordinates between K8s client, containerlab parser, and link state service.
    """

    def __init__(self):
        """Initialize the lab service."""
        self._labs: dict[str, Lab] = {}
        self._status_watchers: dict[str, asyncio.Task] = {}

    async def deploy_lab(self, request: LabDeployRequest) -> LabDeployResponse:
        """
        Deploy a new lab.

        Steps:
        1. Parse containerlab YAML
        2. Create Clabernetes Topology CRD
        3. Extract nodes/links for visualization
        4. Import to LinkStateService with lab prefix
        5. Start status watcher

        Args:
            request: Lab deployment request

        Returns:
            Lab deployment response

        Raises:
            ValueError: If request is invalid
        """
        lab_name = request.name
        namespace = request.namespace

        # Determine containerlab YAML source
        if request.clabernetes_yaml:
            # Parse from full Clabernetes CRD
            crd_name, crd_ns, containerlab_yaml = ContainerlabParser.parse_clabernetes_crd(
                request.clabernetes_yaml
            )
            # Use CRD's name/namespace if not overridden
            if not lab_name:
                lab_name = crd_name
            if not namespace:
                namespace = crd_ns
        elif request.containerlab_yaml:
            containerlab_yaml = request.containerlab_yaml
        else:
            raise ValueError("Either containerlab_yaml or clabernetes_yaml is required")

        # Parse containerlab to extract nodes and links
        nodes, links = ContainerlabParser.parse(containerlab_yaml, lab_name)

        # Create Lab record
        lab = Lab(
            name=lab_name,
            namespace=namespace,
            status=LabStatus.PENDING,
            topology_name=lab_name,
            nodes_count=len(nodes),
            links_count=len(links),
            created_at=datetime.now(),
        )
        self._labs[lab_name] = lab

        # Create Clabernetes Topology CRD in Kubernetes
        try:
            k8s = get_k8s_manager()
            await k8s.create_topology(
                name=lab_name,
                namespace=namespace,
                containerlab_yaml=containerlab_yaml,
                labels={"network-monitor/lab": lab_name},
            )
            lab.status = LabStatus.DEPLOYING
        except ValueError as e:
            # Already exists or other validation error
            lab.status = LabStatus.FAILED
            lab.message = str(e)
            logger.error(f"Failed to create lab {lab_name}: {e}")
        except Exception as e:
            lab.status = LabStatus.FAILED
            lab.message = f"K8s error: {e}"
            logger.error(f"Failed to create lab {lab_name}: {e}")

        # Import nodes and links to visualization
        link_service = get_link_state_service()
        node_ids = []
        link_ids = []

        for node in nodes:
            await link_service.add_node(node)
            node_ids.append(node.id)

        for link in links:
            await link_service.add_link(link)
            link_ids.append(link.id)

        logger.info(
            f"Deployed lab {lab_name}: {len(nodes)} nodes, {len(links)} links"
        )

        # Start status watcher
        if lab.status == LabStatus.DEPLOYING:
            self._start_status_watcher(lab_name, namespace)

        return LabDeployResponse(
            lab=lab_name,
            status=lab.status,
            nodes_discovered=len(nodes),
            links_discovered=len(links),
            topology_crd=lab_name,
            node_ids=node_ids,
            link_ids=link_ids,
            message=lab.message,
        )

    async def get_lab(self, name: str) -> Optional[Lab]:
        """
        Get lab by name.

        Args:
            name: Lab name

        Returns:
            Lab object or None
        """
        lab = self._labs.get(name)
        if lab:
            # Refresh status from K8s
            k8s = get_k8s_manager()
            lab.status = await k8s.get_topology_status(lab.topology_name, lab.namespace)
        return lab

    async def list_labs(self) -> list[Lab]:
        """
        List all labs.

        Returns:
            List of Lab objects
        """
        # Also check K8s for any labs not in memory
        k8s = get_k8s_manager()
        topologies = await k8s.list_topologies()

        for topo in topologies:
            metadata = topo.get("metadata", {})
            name = metadata.get("name", "")
            namespace = metadata.get("namespace", "clab")

            # Check if managed by network-monitor
            labels = metadata.get("labels", {})
            if labels.get("app.kubernetes.io/managed-by") != "network-monitor":
                continue

            if name not in self._labs:
                # Add lab from K8s
                status = await k8s.get_topology_status(name, namespace)
                self._labs[name] = Lab(
                    name=name,
                    namespace=namespace,
                    status=status,
                    topology_name=name,
                    created_at=datetime.fromisoformat(
                        metadata.get("creationTimestamp", datetime.now().isoformat())
                    ),
                )

        return list(self._labs.values())

    async def delete_lab(self, name: str) -> bool:
        """
        Delete a lab.

        Removes:
        - Clabernetes Topology CRD from K8s
        - Nodes and links from visualization
        - Lab record

        Args:
            name: Lab name

        Returns:
            True if deleted, False if not found
        """
        lab = self._labs.get(name)
        if not lab:
            return False

        # Stop status watcher
        if name in self._status_watchers:
            self._status_watchers[name].cancel()
            del self._status_watchers[name]

        # Delete from K8s
        k8s = get_k8s_manager()
        await k8s.delete_topology(lab.topology_name, lab.namespace)

        # Clear from visualization
        link_service = get_link_state_service()
        await link_service.clear_lab(name)

        # Remove lab record
        del self._labs[name]

        logger.info(f"Deleted lab: {name}")
        return True

    async def get_lab_status(self, name: str) -> Optional[LabStatus]:
        """
        Get lab deployment status.

        Args:
            name: Lab name

        Returns:
            LabStatus or None if not found
        """
        lab = await self.get_lab(name)
        return lab.status if lab else None

    async def get_lab_topology(self, name: str) -> Optional[dict]:
        """
        Get lab's nodes and links.

        Args:
            name: Lab name

        Returns:
            Dict with nodes and links, or None if not found
        """
        if name not in self._labs:
            return None

        link_service = get_link_state_service()
        topology = await link_service.get_topology_by_lab(name)
        return topology

    def _start_status_watcher(self, lab_name: str, namespace: str):
        """Start a background task to watch lab status."""
        if lab_name in self._status_watchers:
            return

        async def watch_status():
            k8s = get_k8s_manager()
            try:
                async for status in k8s.watch_topology_status(lab_name, namespace):
                    if lab_name in self._labs:
                        self._labs[lab_name].status = status
                        logger.info(f"Lab {lab_name} status: {status.value}")

                        if status in (LabStatus.RUNNING, LabStatus.FAILED, LabStatus.DELETED):
                            break
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Status watcher error for {lab_name}: {e}")
            finally:
                self._status_watchers.pop(lab_name, None)

        self._status_watchers[lab_name] = asyncio.create_task(watch_status())


# Singleton instance
_lab_service: Optional[LabService] = None


def get_lab_service() -> LabService:
    """Get the singleton LabService instance."""
    global _lab_service
    if _lab_service is None:
        _lab_service = LabService()
    return _lab_service
