"""
Links API routes - Link state and metrics.
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..models.schemas import (
    Link, LinkState, LinkMetrics, LinksResponse, LinkStateEvent
)
from ..services.link_state_service import get_link_state_service

router = APIRouter(prefix="/links", tags=["links"])


@router.get(
    "",
    response_model=LinksResponse,
    summary="Get all links",
    description="Returns all network links with their current state and metrics.",
)
async def get_links(
    state: Optional[LinkState] = Query(None, description="Filter by state"),
):
    """Get all links, optionally filtered by state."""
    service = get_link_state_service()
    links = await service.get_all_links()

    if state:
        links = [l for l in links if l.state == state]

    return LinksResponse(
        links=links,
        count=len(links),
    )


@router.get(
    "/{link_id:path}",
    response_model=Link,
    summary="Get link by ID",
    description="Returns a specific link by its ID.",
)
async def get_link(link_id: str):
    """Get a specific link."""
    service = get_link_state_service()
    link = await service.get_link(link_id)

    if not link:
        raise HTTPException(status_code=404, detail=f"Link not found: {link_id}")

    return link


@router.get(
    "/{link_id:path}/metrics",
    response_model=LinkMetrics,
    summary="Get link metrics",
    description="Returns traffic metrics for a specific link.",
)
async def get_link_metrics(link_id: str):
    """Get metrics for a specific link."""
    service = get_link_state_service()
    link = await service.get_link(link_id)

    if not link:
        raise HTTPException(status_code=404, detail=f"Link not found: {link_id}")

    return link.metrics


@router.put(
    "/{link_id:path}/state",
    response_model=Optional[LinkStateEvent],
    summary="Update link state",
    description="Manually update the state of a link.",
)
async def update_link_state(
    link_id: str,
    state: LinkState,
):
    """Update link state."""
    service = get_link_state_service()
    link = await service.get_link(link_id)

    if not link:
        raise HTTPException(status_code=404, detail=f"Link not found: {link_id}")

    event = await service.update_link_state(link_id, state, source="api")
    return event


@router.put(
    "/{link_id:path}/metrics",
    response_model=Link,
    summary="Update link metrics",
    description="Update traffic metrics for a link.",
)
async def update_link_metrics(
    link_id: str,
    metrics: LinkMetrics,
):
    """Update link metrics."""
    service = get_link_state_service()
    link = await service.get_link(link_id)

    if not link:
        raise HTTPException(status_code=404, detail=f"Link not found: {link_id}")

    await service.update_link_metrics(link_id, metrics)
    return await service.get_link(link_id)


@router.get(
    "/by-interface/{interface}",
    response_model=Link,
    summary="Get link by interface",
    description="Find a link by interface name.",
)
async def get_link_by_interface(interface: str):
    """Get link by interface name."""
    service = get_link_state_service()
    link = await service.get_link_by_interface(interface)

    if not link:
        raise HTTPException(
            status_code=404,
            detail=f"No link found for interface: {interface}"
        )

    return link
