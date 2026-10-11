"""Technical health and readiness endpoints."""
from fastapi import APIRouter, Depends, Response

from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.health_service import HealthService
from app.storage.s3 import S3StorageAdapter, get_storage

router = APIRouter(tags=["Health"])


def get_health_service(
    storage: S3StorageAdapter = Depends(get_storage),
) -> HealthService:
    return HealthService(storage=storage)


@router.get(
    "/health/live",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Checks that the API process is alive and receiving traffic.",
)
async def health_live() -> HealthResponse:
    return HealthService.get_live_status()


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description="Evaluates critical infrastructure readiness (PostgreSQL, MinIO/S3).",
)
async def health_ready(
    response: Response,
    service: HealthService = Depends(get_health_service),
) -> ReadinessResponse:
    status_code, readiness = await service.get_readiness_status()
    response.status_code = status_code
    return readiness
