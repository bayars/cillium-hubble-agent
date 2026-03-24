"""
Interface metrics API routes - Per-interface rx/tx counters from sidecar agents.
"""

from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import (
    InterfaceMetricsPush,
    NodeInterfacesResponse,
)
from ..services.link_state_service import get_link_state_service

router = APIRouter(prefix="/interfaces", tags=["interfaces"])


@router.put(
    "",
    response_model=NodeInterfacesResponse,
    summary="Push interface metrics",
    description="Bulk push per-interface metrics from a sidecar/collector agent. node_id is in the request body.",
)
async def push_interface_metrics(payload: InterfaceMetricsPush):
    """Receive interface metrics from collector agent."""
    service = get_link_state_service()

    ok = await service.update_node_interfaces(payload.node_id, payload.interfaces)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Node {payload.node_id} not found")

    interfaces = await service.get_node_interfaces(payload.node_id)
    return NodeInterfacesResponse(
        node_id=payload.node_id,
        interfaces=interfaces or [],
        count=len(interfaces or []),
    )


@router.get(
    "",
    response_model=NodeInterfacesResponse,
    summary="Get interface metrics for a node",
    description="Get per-interface metrics for a node. Pass node_id as query parameter.",
)
async def get_node_interfaces(
    node_id: str = Query(..., description="Node identifier"),
):
    """Get all interface metrics for a node."""
    service = get_link_state_service()
    interfaces = await service.get_node_interfaces(node_id)

    if interfaces is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")

    node = service._nodes.get(node_id)
    return NodeInterfacesResponse(
        node_id=node_id,
        node_label=node.label if node else "",
        interfaces=interfaces,
        count=len(interfaces),
    )


@router.get(
    "/all",
    summary="Get interface metrics for all nodes",
    description="Get per-interface metrics for all nodes that have reported metrics.",
)
async def get_all_interfaces():
    """Get interface metrics for all nodes."""
    service = get_link_state_service()
    result = []
    for node_id, ifaces in service._node_interfaces.items():
        node = service._nodes.get(node_id)
        result.append(
            NodeInterfacesResponse(
                node_id=node_id,
                node_label=node.label if node else "",
                interfaces=list(ifaces.values()),
                count=len(ifaces),
            )
        )
    return result
