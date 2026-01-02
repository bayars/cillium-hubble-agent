"""
Cilium Endpoint Discovery - Discover and watch network endpoints via Cilium CRDs.

Watches CiliumEndpoint resources to:
- Discover all network endpoints in the cluster
- Detect endpoint additions/removals
- Map endpoints to nodes for topology building
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import AsyncIterator, Callable, Optional

try:
    from kubernetes_asyncio import client, config, watch
    from kubernetes_asyncio.client import ApiException
    K8S_AVAILABLE = True
except ImportError:
    K8S_AVAILABLE = False

logger = logging.getLogger(__name__)


class EndpointEventType(str, Enum):
    """Type of endpoint event."""
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"


class EndpointState(str, Enum):
    """Endpoint state."""
    READY = "ready"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


@dataclass
class CiliumEndpointInfo:
    """Information about a Cilium endpoint."""
    name: str
    namespace: str
    identity: int = 0
    node_name: str = ""
    pod_name: str = ""
    container_id: str = ""
    ipv4_address: str = ""
    ipv6_address: str = ""
    state: EndpointState = EndpointState.UNKNOWN
    labels: dict = field(default_factory=dict)
    security_identity: dict = field(default_factory=dict)
    networking: dict = field(default_factory=dict)
    last_updated: datetime = field(default_factory=datetime.now)

    @property
    def id(self) -> str:
        """Unique endpoint identifier."""
        return f"{self.namespace}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "id": self.id,
            "identity": self.identity,
            "node_name": self.node_name,
            "pod_name": self.pod_name,
            "ipv4_address": self.ipv4_address,
            "ipv6_address": self.ipv6_address,
            "state": self.state.value,
            "labels": self.labels,
            "last_updated": self.last_updated.isoformat(),
        }


@dataclass
class EndpointEvent:
    """Event for endpoint changes."""
    type: EndpointEventType
    endpoint: CiliumEndpointInfo
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "endpoint": self.endpoint.to_dict(),
            "timestamp": self.timestamp.isoformat(),
        }


class CiliumEndpointDiscovery:
    """
    Discover and watch CiliumEndpoint resources.

    Uses the Kubernetes API to watch CiliumEndpoint CRDs and
    track all network endpoints in the cluster.
    """

    CILIUM_API_GROUP = "cilium.io"
    CILIUM_API_VERSION = "v2"
    CILIUM_ENDPOINTS_PLURAL = "ciliumendpoints"

    def __init__(
        self,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
        callback: Optional[Callable[[EndpointEvent], None]] = None,
    ):
        """
        Initialize endpoint discovery.

        Args:
            namespace: Namespace to watch (None for all namespaces)
            label_selector: Optional label selector to filter endpoints
            callback: Optional callback for endpoint events
        """
        if not K8S_AVAILABLE:
            raise RuntimeError(
                "kubernetes-asyncio is required for endpoint discovery. "
                "Install with: pip install kubernetes-asyncio"
            )

        self.namespace = namespace
        self.label_selector = label_selector
        self.callback = callback

        self._running = False
        self._api: Optional[client.CustomObjectsApi] = None
        self._endpoints: dict[str, CiliumEndpointInfo] = {}
        self._event_queue: asyncio.Queue[EndpointEvent] = asyncio.Queue()
        self._watch_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start endpoint discovery."""
        if self._running:
            return

        logger.info("Starting Cilium endpoint discovery...")

        # Load kubernetes config
        try:
            # Try in-cluster config first
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            # Fall back to kubeconfig
            await config.load_kube_config()
            logger.info("Loaded kubeconfig")

        self._api = client.CustomObjectsApi()
        self._running = True

        # List existing endpoints
        await self._list_endpoints()

        # Start watch task
        self._watch_task = asyncio.create_task(self._watch_endpoints())

        logger.info(f"Discovered {len(self._endpoints)} endpoints")

    async def stop(self):
        """Stop endpoint discovery."""
        logger.info("Stopping Cilium endpoint discovery...")
        self._running = False

        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass

        logger.info("Cilium endpoint discovery stopped")

    def _parse_endpoint(self, obj: dict) -> CiliumEndpointInfo:
        """Parse CiliumEndpoint object into EndpointInfo."""
        metadata = obj.get("metadata", {})
        status = obj.get("status", {})
        networking = status.get("networking", {})

        # Get IPv4/IPv6 addresses
        addresses = networking.get("addressing", [])
        ipv4 = ""
        ipv6 = ""
        for addr in addresses:
            if "ipv4" in addr:
                ipv4 = addr["ipv4"]
            if "ipv6" in addr:
                ipv6 = addr["ipv6"]

        # Determine state
        state = EndpointState.UNKNOWN
        if status.get("state") == "ready":
            state = EndpointState.READY
        elif status.get("state") == "not-ready":
            state = EndpointState.NOT_READY

        # Get identity
        identity = status.get("identity", {})
        identity_id = identity.get("id", 0)

        return CiliumEndpointInfo(
            name=metadata.get("name", ""),
            namespace=metadata.get("namespace", "default"),
            identity=identity_id,
            node_name=networking.get("node", ""),
            pod_name=metadata.get("name", ""),  # Usually same as endpoint name
            ipv4_address=ipv4,
            ipv6_address=ipv6,
            state=state,
            labels=identity.get("labels", []),
            security_identity=identity,
            networking=networking,
        )

    async def _list_endpoints(self):
        """List all existing endpoints."""
        try:
            if self.namespace:
                result = await self._api.list_namespaced_custom_object(
                    group=self.CILIUM_API_GROUP,
                    version=self.CILIUM_API_VERSION,
                    namespace=self.namespace,
                    plural=self.CILIUM_ENDPOINTS_PLURAL,
                    label_selector=self.label_selector,
                )
            else:
                result = await self._api.list_cluster_custom_object(
                    group=self.CILIUM_API_GROUP,
                    version=self.CILIUM_API_VERSION,
                    plural=self.CILIUM_ENDPOINTS_PLURAL,
                    label_selector=self.label_selector,
                )

            for item in result.get("items", []):
                endpoint = self._parse_endpoint(item)
                self._endpoints[endpoint.id] = endpoint
                logger.debug(f"Discovered endpoint: {endpoint.id}")

        except ApiException as e:
            logger.error(f"Error listing endpoints: {e}")
            raise

    async def _watch_endpoints(self):
        """Watch for endpoint changes."""
        w = watch.Watch()

        while self._running:
            try:
                if self.namespace:
                    stream = w.stream(
                        self._api.list_namespaced_custom_object,
                        group=self.CILIUM_API_GROUP,
                        version=self.CILIUM_API_VERSION,
                        namespace=self.namespace,
                        plural=self.CILIUM_ENDPOINTS_PLURAL,
                        label_selector=self.label_selector,
                    )
                else:
                    stream = w.stream(
                        self._api.list_cluster_custom_object,
                        group=self.CILIUM_API_GROUP,
                        version=self.CILIUM_API_VERSION,
                        plural=self.CILIUM_ENDPOINTS_PLURAL,
                        label_selector=self.label_selector,
                    )

                async for event in stream:
                    if not self._running:
                        break

                    event_type = event["type"]
                    obj = event["object"]
                    endpoint = self._parse_endpoint(obj)

                    endpoint_event = EndpointEvent(
                        type=EndpointEventType(event_type),
                        endpoint=endpoint,
                    )

                    # Update internal state
                    if event_type == "ADDED" or event_type == "MODIFIED":
                        self._endpoints[endpoint.id] = endpoint
                    elif event_type == "DELETED":
                        self._endpoints.pop(endpoint.id, None)

                    # Emit event
                    await self._event_queue.put(endpoint_event)
                    if self.callback:
                        self.callback(endpoint_event)

                    logger.info(f"Endpoint {event_type}: {endpoint.id}")

            except asyncio.CancelledError:
                break
            except ApiException as e:
                if e.status == 410:  # Gone - resource version too old
                    logger.warning("Watch expired, restarting...")
                    await asyncio.sleep(1)
                    continue
                logger.error(f"Watch error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error in watch: {e}")
                await asyncio.sleep(5)

    async def events(self) -> AsyncIterator[EndpointEvent]:
        """Async iterator for endpoint events."""
        while self._running:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=1.0
                )
                yield event
            except asyncio.TimeoutError:
                continue

    def get_endpoint(self, endpoint_id: str) -> Optional[CiliumEndpointInfo]:
        """Get endpoint by ID (namespace/name)."""
        return self._endpoints.get(endpoint_id)

    def get_all_endpoints(self) -> dict[str, CiliumEndpointInfo]:
        """Get all discovered endpoints."""
        return self._endpoints.copy()

    def get_endpoints_by_node(self, node_name: str) -> list[CiliumEndpointInfo]:
        """Get endpoints on a specific node."""
        return [
            ep for ep in self._endpoints.values()
            if ep.node_name == node_name
        ]

    def get_endpoints_by_namespace(self, namespace: str) -> list[CiliumEndpointInfo]:
        """Get endpoints in a specific namespace."""
        return [
            ep for ep in self._endpoints.values()
            if ep.namespace == namespace
        ]

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def endpoint_count(self) -> int:
        return len(self._endpoints)


# Standalone usage example
async def main():
    """Example usage of CiliumEndpointDiscovery."""
    def on_event(event: EndpointEvent):
        print(f"[{event.type.value}] {event.endpoint.id} - "
              f"IP: {event.endpoint.ipv4_address}, Node: {event.endpoint.node_name}")

    discovery = CiliumEndpointDiscovery(callback=on_event)

    try:
        await discovery.start()
        print(f"Watching {discovery.endpoint_count} endpoints. Press Ctrl+C to stop.")

        async for event in discovery.events():
            print(f"Event: {event.to_dict()}")

    except KeyboardInterrupt:
        pass
    finally:
        await discovery.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
