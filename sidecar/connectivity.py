"""
Clabernetes Connectivity CR reader for the sidecar agent.

Reads the Connectivity custom resource for this topology and builds a reverse
map from VXLAN interface names (as they appear in the launcher pod's netns)
to logical interface names (as defined in the topology).

VXLAN interface naming in Clabernetes:
  vx-{localNode}-{localInterface}
where localInterface has already been sanitized (/ → -) by Clabernetes.

Example:
  CR tunnel: localNode=R1, localInterface=e1-1-c1-1
  VXLAN iface in launcher pod: vx-R1-e1-1-c1-1
  Logical name reported:        e1-1-c1-1  (for node R1)
"""

import logging
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CLABERNETES_GROUP = "clabernetes.containerlab.dev"
CLABERNETES_VERSION = "v1alpha1"
CONNECTIVITY_PLURAL = "connectivities"


@dataclass
class VxlanLink:
    """Maps a VXLAN interface in the launcher pod to a topology link endpoint."""
    vni: int
    node: str           # local node name (e.g. "R1")
    logical_iface: str  # sanitized interface name (e.g. "e1-1-c1-1")
    remote_node: str
    remote_iface: str
    vxlan_iface: str    # Linux interface name in launcher pod (e.g. "vx-R1-e1-1-c1-1")


class ConnectivityResolver:
    """
    Resolves VXLAN interface names to logical topology interface names
    by reading the Clabernetes Connectivity CR.

    Call refresh() periodically to pick up topology changes.
    Fails gracefully — if the K8s API is unavailable, existing map is kept.
    """

    def __init__(self, namespace: str, topology: str, node_name: str):
        self.namespace = namespace
        self.topology = topology
        self.node_name = node_name

        self._lock = threading.RLock()
        # {vxlan_iface_name: VxlanLink}
        self._vxlan_map: dict[str, VxlanLink] = {}
        self._custom = None

    def _init_client(self):
        if self._custom is not None:
            return
        try:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
            self._custom = client.CustomObjectsApi()
        except ImportError:
            raise RuntimeError(
                "kubernetes package required. Install with: pip install kubernetes"
            )

    def refresh(self):
        """Reload the VXLAN→logical name map from the Connectivity CR."""
        try:
            self._init_client()
        except RuntimeError as exc:
            logger.warning("K8s client unavailable: %s", exc)
            return

        try:
            cr = self._custom.get_namespaced_custom_object(
                group=CLABERNETES_GROUP,
                version=CLABERNETES_VERSION,
                namespace=self.namespace,
                plural=CONNECTIVITY_PLURAL,
                name=self.topology,
            )
        except Exception as exc:
            logger.warning(
                "Could not read Connectivity CR %s/%s: %s",
                self.namespace, self.topology, exc,
            )
            return

        new_map: dict[str, VxlanLink] = {}
        tunnels = cr.get("spec", {}).get("tunnels", []) or []

        for tunnel in tunnels:
            local_node = tunnel.get("localNode", "")
            local_iface = tunnel.get("localInterface", "")
            remote_node = tunnel.get("remoteNode", "")
            remote_iface = tunnel.get("remoteInterface", "")
            vni = tunnel.get("tunnelID", 0)

            if local_node != self.node_name:
                continue
            if not all([local_node, local_iface, remote_node, remote_iface, vni]):
                continue

            vxlan_iface = f"vx-{local_node}-{local_iface}"
            link = VxlanLink(
                vni=vni,
                node=local_node,
                logical_iface=local_iface,
                remote_node=remote_node,
                remote_iface=remote_iface,
                vxlan_iface=vxlan_iface,
            )
            new_map[vxlan_iface] = link
            logger.debug(
                "VNI %d: %s → %s (remote: %s/%s)",
                vni, vxlan_iface, local_iface, remote_node, remote_iface,
            )

        with self._lock:
            self._vxlan_map = new_map

        logger.info(
            "Connectivity CR refreshed: %d VXLAN links for node %s",
            len(new_map), self.node_name,
        )

    def resolve_vxlan(self, vxlan_iface: str) -> Optional[VxlanLink]:
        """Return VxlanLink for a VXLAN interface name, or None if unknown."""
        with self._lock:
            return self._vxlan_map.get(vxlan_iface)

    @property
    def vxlan_ifaces(self) -> set[str]:
        """Set of known VXLAN interface names for this node."""
        with self._lock:
            return set(self._vxlan_map.keys())

    @property
    def link_count(self) -> int:
        with self._lock:
            return len(self._vxlan_map)


def refresh_loop(resolver: ConnectivityResolver, interval_s: int, stop_event: threading.Event):
    """Background thread: refresh ConnectivityResolver every interval_s seconds."""
    while not stop_event.is_set():
        stop_event.wait(interval_s)
        if stop_event.is_set():
            break
        try:
            resolver.refresh()
        except Exception as exc:
            logger.warning("Connectivity refresh failed: %s", exc)
