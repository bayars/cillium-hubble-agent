"""
Labs API Routes - Lab lifecycle management endpoints.

Provides REST API for:
- Deploying labs (creates Clabernetes Topology CRD)
- Listing labs
- Getting lab status and topology
- Deleting labs
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Query

from ..models.schemas import (
    Lab,
    LabStatus,
    LabDeployRequest,
    LabDeployResponse,
    LabListResponse,
)
from ..services.lab_service import get_lab_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/labs", tags=["labs"])


@router.post(
    "",
    response_model=LabDeployResponse,
    summary="Deploy a new lab",
    description="Deploy a lab by creating a Clabernetes Topology CRD in Kubernetes.",
)
async def deploy_lab(request: LabDeployRequest):
    """
    Deploy a new lab.

    The request can include either:
    - containerlab_yaml: Just the containerlab topology definition
    - clabernetes_yaml: Full Clabernetes Topology CRD

    The topology will be parsed to extract nodes and links for visualization,
    and a Clabernetes Topology CRD will be created in Kubernetes.
    """
    if not request.containerlab_yaml and not request.clabernetes_yaml:
        raise HTTPException(
            status_code=400,
            detail="Either containerlab_yaml or clabernetes_yaml is required",
        )

    service = get_lab_service()
    try:
        result = await service.deploy_lab(request)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to deploy lab: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {e}")


@router.post(
    "/file",
    response_model=LabDeployResponse,
    summary="Deploy lab from YAML file",
    description="Upload a containerlab or Clabernetes YAML file to deploy a lab.",
)
async def deploy_lab_from_file(
    file: UploadFile = File(...),
    name: Optional[str] = Query(None, description="Lab name (extracted from YAML if not provided)"),
    namespace: str = Query("clab", description="Kubernetes namespace"),
):
    """Deploy a lab from uploaded YAML file."""
    try:
        content = await file.read()
        yaml_content = content.decode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read file: {e}")

    # Determine if it's a Clabernetes CRD or just containerlab
    is_crd = "apiVersion:" in yaml_content and "clabernetes" in yaml_content.lower()

    request = LabDeployRequest(
        name=name or "",
        namespace=namespace,
        clabernetes_yaml=yaml_content if is_crd else None,
        containerlab_yaml=yaml_content if not is_crd else None,
    )

    service = get_lab_service()
    try:
        result = await service.deploy_lab(request)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to deploy lab from file: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {e}")


@router.get(
    "",
    response_model=LabListResponse,
    summary="List all labs",
    description="Get a list of all deployed labs.",
)
async def list_labs():
    """List all labs."""
    service = get_lab_service()
    labs = await service.list_labs()
    return LabListResponse(
        labs=labs,
        count=len(labs),
    )


@router.get(
    "/{name}",
    response_model=Lab,
    summary="Get lab details",
    description="Get details of a specific lab including its current status.",
)
async def get_lab(name: str):
    """Get lab by name."""
    service = get_lab_service()
    lab = await service.get_lab(name)
    if not lab:
        raise HTTPException(status_code=404, detail=f"Lab '{name}' not found")
    return lab


@router.get(
    "/{name}/status",
    summary="Get lab status",
    description="Get the deployment status of a lab.",
)
async def get_lab_status(name: str):
    """Get lab deployment status."""
    service = get_lab_service()
    status = await service.get_lab_status(name)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Lab '{name}' not found")
    return {"lab": name, "status": status.value}


@router.get(
    "/{name}/topology",
    summary="Get lab topology",
    description="Get the nodes and links for a specific lab.",
)
async def get_lab_topology(name: str):
    """Get lab's nodes and links for visualization."""
    service = get_lab_service()
    topology = await service.get_lab_topology(name)
    if topology is None:
        raise HTTPException(status_code=404, detail=f"Lab '{name}' not found")
    return topology


@router.delete(
    "/{name}",
    summary="Delete lab",
    description="Delete a lab and remove its Clabernetes CRD from Kubernetes.",
)
async def delete_lab(name: str):
    """
    Delete a lab.

    This will:
    - Remove the Clabernetes Topology CRD from Kubernetes
    - Remove nodes and links from visualization
    - Remove the lab record
    """
    service = get_lab_service()
    deleted = await service.delete_lab(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Lab '{name}' not found")
    return {"status": "deleted", "lab": name}
