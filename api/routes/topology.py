"""
Topology API routes - Full network graph for Cytoscape.
"""

from fastapi import APIRouter, HTTPException

from ..models.schemas import TopologyResponse, Node, Link
from ..services.link_state_service import get_link_state_service

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get(
    "",
    response_model=TopologyResponse,
    summary="Get network topology",
    description="Returns the complete network topology with nodes and edges for Cytoscape visualization.",
)
async def get_topology():
    """Get complete network topology."""
    service = get_link_state_service()
    return await service.get_topology()


@router.post(
    "/nodes",
    response_model=Node,
    summary="Add a node",
    description="Add a new node to the topology.",
)
async def add_node(node: Node):
    """Add a node to the topology."""
    service = get_link_state_service()
    await service.add_node(node)
    return node


@router.post(
    "/links",
    response_model=Link,
    summary="Add a link",
    description="Add a new link to the topology.",
)
async def add_link(link: Link):
    """Add a link to the topology."""
    service = get_link_state_service()
    await service.add_link(link)
    return link


@router.delete(
    "/nodes/{node_id}",
    summary="Remove a node",
    description="Remove a node from the topology.",
)
async def remove_node(node_id: str):
    """Remove a node from the topology."""
    service = get_link_state_service()
    await service.remove_node(node_id)
    return {"status": "removed", "node_id": node_id}


@router.delete(
    "/links/{link_id}",
    summary="Remove a link",
    description="Remove a link from the topology.",
)
async def remove_link(link_id: str):
    """Remove a link from the topology."""
    service = get_link_state_service()
    await service.remove_link(link_id)
    return {"status": "removed", "link_id": link_id}
