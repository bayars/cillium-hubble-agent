"""
Network Monitor API - FastAPI Application.

Provides REST API and WebSocket endpoints for:
- Network topology visualization (Cytoscape)
- Link state monitoring
- Real-time event streaming
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .models.schemas import (
    HealthResponse, ErrorResponse, TopologyResponse,
    Node, Link, LinkState, NodeStatus,
)
from .routes import topology, links, websocket, events, labs
from .services.link_state_service import get_link_state_service
from .services.event_bus import get_event_bus

logger = logging.getLogger(__name__)

# Application start time
START_TIME = datetime.now()


def setup_logging():
    """Configure logging."""
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def initialize_demo_topology():
    """Initialize with demo topology for testing."""
    service = get_link_state_service()

    # Demo nodes
    nodes = [
        Node(id="router1", label="R1", type="router", status=NodeStatus.UP, platform="srlinux"),
        Node(id="router2", label="R2", type="router", status=NodeStatus.UP, platform="ceos"),
        Node(id="router3", label="R3", type="router", status=NodeStatus.UP, platform="frr"),
        Node(id="switch1", label="SW1", type="switch", status=NodeStatus.UP),
    ]

    # Demo links
    links = [
        Link(
            id="link1",
            source="router1",
            target="router2",
            source_interface="ethernet-1/1",
            target_interface="Ethernet1",
            state=LinkState.ACTIVE,
            speed_mbps=10000,
        ),
        Link(
            id="link2",
            source="router2",
            target="router3",
            source_interface="Ethernet2",
            target_interface="eth0",
            state=LinkState.IDLE,
            speed_mbps=1000,
        ),
        Link(
            id="link3",
            source="router3",
            target="switch1",
            source_interface="eth1",
            target_interface="eth1",
            state=LinkState.ACTIVE,
            speed_mbps=1000,
        ),
        Link(
            id="link4",
            source="switch1",
            target="router1",
            source_interface="eth2",
            target_interface="ethernet-1/2",
            state=LinkState.DOWN,
            speed_mbps=10000,
        ),
    ]

    await service.initialize_topology(nodes, links)
    logger.info("Demo topology initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    setup_logging()
    logger.info("Starting Network Monitor API...")

    # Initialize demo topology if enabled
    if os.environ.get("DEMO_MODE", "true").lower() == "true":
        await initialize_demo_topology()

    logger.info("Network Monitor API started")
    yield
    logger.info("Shutting down Network Monitor API...")


# Create FastAPI app
app = FastAPI(
    title="Network Monitor API",
    description="""
    API for network topology monitoring and real-time link state tracking.

    ## Features
    - **Topology**: Get complete network graph for Cytoscape visualization
    - **Links**: Query and update link states and metrics
    - **Labs**: Deploy and manage Clabernetes labs with auto-topology parsing
    - **Events**: Submit state change events from monitoring agents
    - **WebSocket**: Real-time event streaming for live updates

    ## Link States
    - `active`: Link is up and traffic is flowing
    - `idle`: Link is up but no traffic
    - `down`: Link is down
    - `unknown`: State not yet determined
    """,
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(topology.router, prefix="/api")
app.include_router(links.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(labs.router, prefix="/api")
app.include_router(websocket.router)


# Health check endpoint
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["health"],
    summary="Health check",
)
async def health_check():
    """Health check endpoint."""
    from .routes.websocket import get_connection_manager

    service = get_link_state_service()
    stats = service.get_stats()
    ws_manager = get_connection_manager()

    return HealthResponse(
        status="healthy",
        version="1.0.0",
        uptime_seconds=(datetime.now() - START_TIME).total_seconds(),
        connected_agents=ws_manager.connection_count,
        monitored_links=stats["link_count"],
    )


# Root endpoint
@app.get("/", tags=["root"])
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Network Monitor API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "topology": "/api/topology",
            "links": "/api/links",
            "events": "/api/events",
            "labs": "/api/labs",
            "websocket": "/ws/events",
        },
    }


# Error handlers
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="Internal server error",
            detail=str(exc),
        ).model_dump(mode='json'),
    )


# Main entry point
if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    uvicorn.run(
        "api.main:app",
        host=host,
        port=port,
        reload=True,
        log_level="info",
    )
