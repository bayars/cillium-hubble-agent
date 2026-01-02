"""
WebSocket routes - Real-time event streaming.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from ..models.schemas import InterfaceEvent
from ..services.event_bus import get_event_bus, Subscriber
from ..services.link_state_service import get_link_state_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manages WebSocket connections."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"WebSocket connected (total: {len(self.active_connections)})")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected (total: {len(self.active_connections)})")

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients."""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)


manager = ConnectionManager()


@router.websocket("/ws/events")
async def websocket_events(
    websocket: WebSocket,
    event_types: Optional[str] = Query(None, description="Comma-separated event types"),
):
    """
    WebSocket endpoint for real-time event streaming.

    Clients receive events as they occur. Optionally filter by event type.

    Event types:
    - link_state_change: Link state changed (active/idle/down)
    - node_added: New node added
    - node_removed: Node removed
    - link_added: New link added
    - link_removed: Link removed
    """
    await manager.connect(websocket)

    # Parse event type filter
    type_filter = None
    if event_types:
        type_filter = [t.strip() for t in event_types.split(",")]

    # Subscribe to event bus
    event_bus = get_event_bus()
    subscriber = await event_bus.subscribe(type_filter)

    try:
        # Send initial state
        service = get_link_state_service()
        topology = await service.get_topology()
        await websocket.send_json({
            "type": "initial_state",
            "data": topology.model_dump(mode='json'),
            "timestamp": datetime.now().isoformat(),
        })

        # Stream events
        while True:
            # Check for incoming messages (ping/pong, commands)
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=0.1
                )
                # Handle client messages if needed
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    pass
            except asyncio.TimeoutError:
                pass

            # Get events from bus
            event = await subscriber.get_event(timeout=1.0)
            if event:
                await websocket.send_json(event.to_dict())

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await event_bus.unsubscribe(subscriber)
        manager.disconnect(websocket)


@router.websocket("/ws/agent")
async def websocket_agent(websocket: WebSocket):
    """
    WebSocket endpoint for monitoring agents.

    Agents connect here to push state change events.
    """
    await websocket.accept()
    logger.info("Agent connected via WebSocket")

    service = get_link_state_service()

    try:
        while True:
            data = await websocket.receive_json()

            # Parse agent event
            try:
                event = InterfaceEvent(**data)
                await service.handle_agent_event(event)

                await websocket.send_json({
                    "status": "ok",
                    "message": f"Processed event for {event.interface}",
                })

            except Exception as e:
                logger.error(f"Error processing agent event: {e}")
                await websocket.send_json({
                    "status": "error",
                    "message": str(e),
                })

    except WebSocketDisconnect:
        logger.info("Agent disconnected")
    except Exception as e:
        logger.error(f"Agent WebSocket error: {e}")


def get_connection_manager() -> ConnectionManager:
    """Get the connection manager instance."""
    return manager
