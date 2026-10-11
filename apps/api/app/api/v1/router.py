"""Version 1 API router."""
from fastapi import APIRouter, Depends, Response

from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.health_service import HealthService
from app.storage.s3 import S3StorageAdapter, get_storage

api_v1_router = APIRouter(prefix="/api/v1", tags=["v1"])


def get_health_service(
    storage: S3StorageAdapter = Depends(get_storage),
) -> HealthService:
    return HealthService(storage=storage)


@api_v1_router.get(
    "/health",
    response_model=HealthResponse,
    summary="API v1 liveness",
    description="Returns API status for frontend client connectivity.",
)
async def api_health() -> HealthResponse:
    return HealthService.get_live_status()


@api_v1_router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="API v1 readiness",
    description="Returns infrastructure readiness for API clients.",
)
async def api_ready(
    response: Response,
    service: HealthService = Depends(get_health_service),
) -> ReadinessResponse:
    status_code, readiness = await service.get_readiness_status()
    response.status_code = status_code
    return readiness
