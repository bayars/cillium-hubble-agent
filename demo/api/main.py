"""
Network Monitor API - FastAPI Application.

Provides REST API and WebSocket endpoints for:
- Network topology visualization (Cytoscape)
- Link state monitoring (via Cilium Hubble)
- Real-time event streaming
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .models.schemas import HealthResponse, ErrorResponse
from .routes import topology as topology, links as links, websocket as websocket, events as events, labs as labs, interfaces as interfaces
from .services.link_state_service import get_link_state_service

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    setup_logging()
    logger.info("Starting Network Monitor API...")

    # Start Hubble integration if enabled
    hubble_enabled = os.environ.get("HUBBLE_ENABLED", "false").lower() == "true"
    if hubble_enabled:
        from .services.hubble_service import start_hubble_service
        try:
            await start_hubble_service()
        except Exception as e:
            logger.error(f"Failed to start Hubble service: {e}")
            logger.warning("Continuing without Hubble integration")

    logger.info("Network Monitor API started")
    yield

    # Shutdown
    if hubble_enabled:
        from .services.hubble_service import stop_hubble_service
        await stop_hubble_service()

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
    - **Events**: Submit state change events
    - **WebSocket**: Real-time event streaming for live updates
    - **Hubble**: Direct Cilium Hubble integration for flow monitoring

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
app.include_router(interfaces.router, prefix="/api")
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
    from .services.hubble_service import get_hubble_service

    service = get_link_state_service()
    stats = service.get_stats()
    ws_manager = get_connection_manager()
    hubble = get_hubble_service()

    return HealthResponse(
        status="healthy",
        version="1.0.0",
        uptime_seconds=(datetime.now() - START_TIME).total_seconds(),
        connected_clients=ws_manager.connection_count,
        monitored_links=stats["link_count"],
        hubble_connected=hubble.is_running if hubble else False,
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
