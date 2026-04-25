"""
Clabernetes topology resolver.

Reads Clabernetes Connectivity CRs and launcher pod labels to build:
  - vni_to_link:  {vni: LinkEndpoints}
  - pod_to_node:  {pod_name: NodeInfo}

These two maps let the collector translate a Hubble flow
(src_pod, dst_pod, tunnel_vni) → (topology, node, sr_os_iface).

Refreshed periodically so link additions/removals are picked up.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

CLABERNETES_GROUP = "clabernetes.containerlab.dev"
CLABERNETES_VERSION = "v1alpha1"
CONNECTIVITY_PLURAL = "connectivities"

# Clabernetes labels on launcher pods
LABEL_TOPOLOGY = "clabernetes/topologyOwner"
LABEL_NODE = "clabernetes/topologyNode"
LABEL_APP = "clabernetes/app"


@dataclass
class NodeInfo:
    """Maps a launcher pod to a topology node."""
    pod_name: str
    namespace: str
    topology: str       # clabernetes/topologyOwner label value
    node: str           # clabernetes/topologyNode label value
    pod_ip: str = ""

    @property
    def node_id(self) -> str:
        return f"{self.namespace}/{self.topology}/{self.node}"


@dataclass
class LinkEndpoint:
    """One side of a topology link."""
    namespace: str
    topology: str
    node: str           # topology node name (e.g. R1)
    iface: str          # mapped Linux iface name (e.g. e1-1-1 or e1-1-c1-1)

    @property
    def node_id(self) -> str:
        return f"{self.namespace}/{self.topology}/{self.node}"

    @property
    def redis_iface(self) -> str:
        """Interface name safe for Redis keys (slashes already replaced in mapped names)."""
        return self.iface.replace("/", "-")


@dataclass
class LinkEndpoints:
    """Both sides of a topology link identified by a VXLAN VNI."""
    vni: int
    local: LinkEndpoint
    remote: LinkEndpoint


class TopologyResolver:
    """
    Resolves Clabernetes topology information from the Kubernetes API.

    Call refresh() periodically to pick up changes.
    """

    def __init__(self, namespace: Optional[str] = None):
        """
        Args:
            namespace: If set, only watch topologies in this namespace.
                       If None, watch cluster-wide.
        """
        self.namespace = namespace
        self._v1 = None
        self._custom = None

        # {vni: LinkEndpoints}
        self._vni_map: dict[int, LinkEndpoints] = {}
        # {pod_name: NodeInfo}  — keyed by pod name within its namespace
        self._pod_map: dict[str, NodeInfo] = {}

    def _init_client(self):
        if self._v1 is not None:
            return
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._v1 = client.CoreV1Api()
            self._custom = client.CustomObjectsApi()
        except ImportError:
            raise RuntimeError(
                "kubernetes package required. Install with: pip install kubernetes"
            )

    def refresh(self):
        """Reload VNI map and pod map from the Kubernetes API."""
        self._init_client()
        self._refresh_connectivity()
        self._refresh_pods()
        logger.info(
            "Topology refresh: %d VNI entries, %d pods",
            len(self._vni_map),
            len(self._pod_map),
        )

    def _refresh_connectivity(self):
        """Read all Connectivity CRs and rebuild the VNI → link map."""
        try:
            if self.namespace:
                result = self._custom.list_namespaced_custom_object(
                    group=CLABERNETES_GROUP,
                    version=CLABERNETES_VERSION,
                    namespace=self.namespace,
                    plural=CONNECTIVITY_PLURAL,
                )
            else:
                result = self._custom.list_cluster_custom_object(
                    group=CLABERNETES_GROUP,
                    version=CLABERNETES_VERSION,
                    plural=CONNECTIVITY_PLURAL,
                )
        except Exception as exc:
            logger.warning("Could not list Connectivity CRs: %s", exc)
            return

        new_map: dict[int, LinkEndpoints] = {}

        for item in result.get("items", []):
            meta = item.get("metadata", {})
            ns = meta.get("namespace", "default")
            # Connectivity CR name = topology name in Clabernetes
            topology = meta.get("name", "")

            spec = item.get("spec", {})
            tunnels = spec.get("tunnels", []) or []

            for tunnel in tunnels:
                vni = tunnel.get("tunnelID", 0)
                if not vni:
                    continue

                local_node = tunnel.get("localNode", "")
                local_iface = tunnel.get("localInterface", "")
                remote_node = tunnel.get("remoteNode", "")
                remote_iface = tunnel.get("remoteInterface", "")

                if not all([local_node, local_iface, remote_node, remote_iface]):
                    continue

                link = LinkEndpoints(
                    vni=vni,
                    local=LinkEndpoint(
                        namespace=ns,
                        topology=topology,
                        node=local_node,
                        iface=local_iface,
                    ),
                    remote=LinkEndpoint(
                        namespace=ns,
                        topology=topology,
                        node=remote_node,
                        iface=remote_iface,
                    ),
                )
                new_map[vni] = link
                logger.debug(
                    "VNI %d: %s/%s → %s/%s",
                    vni, local_node, local_iface, remote_node, remote_iface,
                )

        self._vni_map = new_map

    def _refresh_pods(self):
        """Read launcher pods and rebuild the pod → NodeInfo map."""
        label_selector = f"{LABEL_APP}=clabernetes"

        try:
            if self.namespace:
                pods = self._v1.list_namespaced_pod(
                    namespace=self.namespace,
                    label_selector=label_selector,
                )
            else:
                pods = self._v1.list_pod_for_all_namespaces(
                    label_selector=label_selector,
                )
        except Exception as exc:
            logger.warning("Could not list launcher pods: %s", exc)
            return

        new_map: dict[str, NodeInfo] = {}

        for pod in pods.items:
            meta = pod.metadata
            labels = meta.labels or {}
            topology = labels.get(LABEL_TOPOLOGY, "")
            node = labels.get(LABEL_NODE, "")
            if not topology or not node:
                continue

            pod_ip = ""
            if pod.status and pod.status.pod_ip:
                pod_ip = pod.status.pod_ip

            info = NodeInfo(
                pod_name=meta.name,
                namespace=meta.namespace,
                topology=topology,
                node=node,
                pod_ip=pod_ip,
            )
            # Key by pod_name (unique within namespace)
            new_map[meta.name] = info
            logger.debug(
                "Pod %s → %s/%s/%s (ip=%s)",
                meta.name, meta.namespace, topology, node, pod_ip,
            )

        self._pod_map = new_map

    def resolve_vni(self, vni: int) -> Optional[LinkEndpoints]:
        """Return link endpoints for a VNI, or None if unknown."""
        return self._vni_map.get(vni)

    def resolve_pod(self, pod_name: str) -> Optional[NodeInfo]:
        """Return NodeInfo for a launcher pod name, or None if unknown."""
        return self._pod_map.get(pod_name)

    def resolve_pod_by_ip(self, ip: str) -> Optional[NodeInfo]:
        """Return NodeInfo for a launcher pod IP, or None if unknown."""
        for info in self._pod_map.values():
            if info.pod_ip == ip:
                return info
        return None

    @property
    def vni_count(self) -> int:
        return len(self._vni_map)

    @property
    def pod_count(self) -> int:
        return len(self._pod_map)
