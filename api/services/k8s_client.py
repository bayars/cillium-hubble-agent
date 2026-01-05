"""
Kubernetes Client for Clabernetes Topology Management.

Manages Clabernetes Topology CRDs in Kubernetes for lab lifecycle operations.
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

import yaml

try:
    from kubernetes_asyncio import client, config, watch
    from kubernetes_asyncio.client import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

from ..models.schemas import LabStatus

logger = logging.getLogger(__name__)


class K8sLabManager:
    """
    Manages Clabernetes Topology CRDs in Kubernetes.

    Provides CRUD operations for lab topologies and status monitoring.
    """

    CLABERNETES_API_GROUP = "clabernetes.containerlab.dev"
    CLABERNETES_API_VERSION = "v1alpha1"
    CLABERNETES_PLURAL = "topologies"

    def __init__(self):
        """Initialize the Kubernetes client."""
        if not K8S_AVAILABLE:
            raise RuntimeError(
                "kubernetes-asyncio is required. "
                "Install with: pip install kubernetes-asyncio"
            )

        self._initialized = False
        self._api: Optional[client.CustomObjectsApi] = None
        self._core_api: Optional[client.CoreV1Api] = None

    async def initialize(self):
        """Initialize Kubernetes client and load config."""
        if self._initialized:
            return

        try:
            # Try in-cluster config first
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            # Fall back to kubeconfig
            await config.load_kube_config()
            logger.info("Loaded kubeconfig")

        self._api = client.CustomObjectsApi()
        self._core_api = client.CoreV1Api()
        self._initialized = True

    async def create_topology(
        self,
        name: str,
        namespace: str,
        containerlab_yaml: str,
        labels: Optional[dict] = None,
    ) -> dict:
        """
        Create a Clabernetes Topology CRD.

        Args:
            name: Topology name
            namespace: Target namespace
            containerlab_yaml: Containerlab topology definition
            labels: Optional labels to add

        Returns:
            Created topology object

        Raises:
            ApiException: If creation fails
        """
        await self.initialize()

        # Build the Topology CRD
        topology = {
            "apiVersion": f"{self.CLABERNETES_API_GROUP}/{self.CLABERNETES_API_VERSION}",
            "kind": "Topology",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": {
                    "app.kubernetes.io/name": name,
                    "app.kubernetes.io/managed-by": "network-monitor",
                    **(labels or {}),
                },
            },
            "spec": {
                "naming": "prefixed",
                "expose": {
                    "disableAutoExpose": False,
                    "exposeType": "ClusterIP",
                },
                "definition": {
                    "containerlab": containerlab_yaml,
                },
            },
        }

        try:
            # Ensure namespace exists
            await self._ensure_namespace(namespace)

            # Create the topology
            result = await self._api.create_namespaced_custom_object(
                group=self.CLABERNETES_API_GROUP,
                version=self.CLABERNETES_API_VERSION,
                namespace=namespace,
                plural=self.CLABERNETES_PLURAL,
                body=topology,
            )
            logger.info(f"Created Clabernetes topology: {namespace}/{name}")
            return result

        except ApiException as e:
            if e.status == 409:
                logger.warning(f"Topology {namespace}/{name} already exists")
                raise ValueError(f"Topology '{name}' already exists in namespace '{namespace}'")
            logger.error(f"Failed to create topology: {e}")
            raise

    async def get_topology(self, name: str, namespace: str) -> Optional[dict]:
        """
        Get a Clabernetes Topology.

        Args:
            name: Topology name
            namespace: Namespace

        Returns:
            Topology object or None if not found
        """
        await self.initialize()

        try:
            result = await self._api.get_namespaced_custom_object(
                group=self.CLABERNETES_API_GROUP,
                version=self.CLABERNETES_API_VERSION,
                namespace=namespace,
                plural=self.CLABERNETES_PLURAL,
                name=name,
            )
            return result
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    async def delete_topology(self, name: str, namespace: str) -> bool:
        """
        Delete a Clabernetes Topology.

        Args:
            name: Topology name
            namespace: Namespace

        Returns:
            True if deleted, False if not found
        """
        await self.initialize()

        try:
            await self._api.delete_namespaced_custom_object(
                group=self.CLABERNETES_API_GROUP,
                version=self.CLABERNETES_API_VERSION,
                namespace=namespace,
                plural=self.CLABERNETES_PLURAL,
                name=name,
            )
            logger.info(f"Deleted Clabernetes topology: {namespace}/{name}")
            return True
        except ApiException as e:
            if e.status == 404:
                logger.warning(f"Topology {namespace}/{name} not found")
                return False
            raise

    async def list_topologies(self, namespace: Optional[str] = None) -> list[dict]:
        """
        List Clabernetes Topologies.

        Args:
            namespace: Namespace to list from (None for all namespaces)

        Returns:
            List of topology objects
        """
        await self.initialize()

        try:
            if namespace:
                result = await self._api.list_namespaced_custom_object(
                    group=self.CLABERNETES_API_GROUP,
                    version=self.CLABERNETES_API_VERSION,
                    namespace=namespace,
                    plural=self.CLABERNETES_PLURAL,
                )
            else:
                result = await self._api.list_cluster_custom_object(
                    group=self.CLABERNETES_API_GROUP,
                    version=self.CLABERNETES_API_VERSION,
                    plural=self.CLABERNETES_PLURAL,
                )
            return result.get("items", [])
        except ApiException as e:
            logger.error(f"Failed to list topologies: {e}")
            return []

    async def get_topology_status(self, name: str, namespace: str) -> LabStatus:
        """
        Get the deployment status of a topology.

        Args:
            name: Topology name
            namespace: Namespace

        Returns:
            LabStatus enum value
        """
        await self.initialize()

        topology = await self.get_topology(name, namespace)
        if not topology:
            return LabStatus.DELETED

        status = topology.get("status", {})
        conditions = status.get("conditions", [])

        # Parse conditions to determine status
        ready = False
        progressing = False

        for condition in conditions:
            cond_type = condition.get("type", "")
            cond_status = condition.get("status", "") == "True"

            if cond_type == "Ready" and cond_status:
                ready = True
            if cond_type == "Progressing" and cond_status:
                progressing = True

        if ready:
            return LabStatus.RUNNING
        if progressing:
            return LabStatus.DEPLOYING

        # Check if there's a failure
        for condition in conditions:
            if condition.get("type") == "Ready":
                reason = condition.get("reason", "")
                if "Failed" in reason or "Error" in reason:
                    return LabStatus.FAILED

        return LabStatus.PENDING

    async def get_topology_pods(self, name: str, namespace: str) -> list[dict]:
        """
        Get pods belonging to a topology.

        Args:
            name: Topology name
            namespace: Namespace

        Returns:
            List of pod objects
        """
        await self.initialize()

        try:
            # Clabernetes labels pods with the topology name
            label_selector = f"clabernetes/topologyName={name}"
            result = await self._core_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=label_selector,
            )
            return [pod.to_dict() for pod in result.items]
        except ApiException as e:
            logger.error(f"Failed to get pods for topology {name}: {e}")
            return []

    async def watch_topology_status(
        self,
        name: str,
        namespace: str,
        timeout: int = 300,
    ) -> AsyncIterator[LabStatus]:
        """
        Watch topology status changes.

        Args:
            name: Topology name
            namespace: Namespace
            timeout: Watch timeout in seconds

        Yields:
            LabStatus on each change
        """
        await self.initialize()

        w = watch.Watch()

        try:
            async for event in w.stream(
                self._api.list_namespaced_custom_object,
                group=self.CLABERNETES_API_GROUP,
                version=self.CLABERNETES_API_VERSION,
                namespace=namespace,
                plural=self.CLABERNETES_PLURAL,
                field_selector=f"metadata.name={name}",
                timeout_seconds=timeout,
            ):
                event_type = event.get("type")
                obj = event.get("object", {})

                if event_type == "DELETED":
                    yield LabStatus.DELETED
                    break

                # Get current status
                status = await self.get_topology_status(name, namespace)
                yield status

                # Stop watching if terminal state
                if status in (LabStatus.RUNNING, LabStatus.FAILED):
                    break

        except asyncio.CancelledError:
            pass
        finally:
            w.stop()

    async def _ensure_namespace(self, namespace: str):
        """Ensure namespace exists, create if not."""
        try:
            await self._core_api.read_namespace(name=namespace)
        except ApiException as e:
            if e.status == 404:
                ns = client.V1Namespace(
                    metadata=client.V1ObjectMeta(
                        name=namespace,
                        labels={"app.kubernetes.io/managed-by": "network-monitor"},
                    )
                )
                await self._core_api.create_namespace(body=ns)
                logger.info(f"Created namespace: {namespace}")
            else:
                raise


# Singleton instance
_k8s_manager: Optional[K8sLabManager] = None


def get_k8s_manager() -> K8sLabManager:
    """Get the singleton K8sLabManager instance."""
    global _k8s_manager
    if _k8s_manager is None:
        _k8s_manager = K8sLabManager()
    return _k8s_manager
