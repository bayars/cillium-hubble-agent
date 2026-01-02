"""
Events API routes - Event ingestion from agents.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException

from ..models.schemas import InterfaceEvent, LinkStateEvent
from ..services.link_state_service import get_link_state_service
from ..services.event_bus import get_event_bus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


@router.post(
    "",
    response_model=Optional[LinkStateEvent],
    summary="Submit event",
    description="Submit a state change event from a monitoring agent.",
)
async def submit_event(event: InterfaceEvent):
    """
    Submit a state change event from agent.

    This endpoint is called by monitoring agents to report
    interface state changes (link up/down, traffic active/idle).
    """
    logger.debug(f"Received event: {event.interface} -> {event.new_state}")

    service = get_link_state_service()
    result = await service.handle_agent_event(event)

    return result


@router.post(
    "/batch",
    summary="Submit batch of events",
    description="Submit multiple events at once.",
)
async def submit_batch(events: list[InterfaceEvent]):
    """Submit a batch of events."""
    service = get_link_state_service()
    results = []

    for event in events:
        try:
            result = await service.handle_agent_event(event)
            results.append({
                "interface": event.interface,
                "processed": True,
                "state_changed": result is not None,
            })
        except Exception as e:
            logger.error(f"Error processing event for {event.interface}: {e}")
            results.append({
                "interface": event.interface,
                "processed": False,
                "error": str(e),
            })

    return {
        "processed": len([r for r in results if r["processed"]]),
        "failed": len([r for r in results if not r["processed"]]),
        "results": results,
    }


@router.get(
    "/history",
    summary="Get event history",
    description="Get recent events from history.",
)
async def get_event_history(
    event_type: Optional[str] = None,
    limit: int = 100,
):
    """Get recent events from history."""
    event_bus = get_event_bus()
    events = event_bus.get_history(event_type, limit)

    return {
        "events": [e.to_dict() for e in events],
        "count": len(events),
    }
