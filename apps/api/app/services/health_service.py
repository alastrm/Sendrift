"""Health and readiness evaluation service."""
from datetime import datetime, timezone

from app.core.database import check_db_health
from app.schemas.health import HealthResponse, ReadinessResponse
from app.storage.s3 import S3StorageAdapter, get_storage


class HealthService:
    """Service to evaluate service liveness and readiness."""

    def __init__(self, storage: S3StorageAdapter | None = None) -> None:
        self.storage = storage or get_storage()

    @staticmethod
    def get_live_status() -> HealthResponse:
        return HealthResponse(
            status="ok",
            app="Sendrift API",
            version="0.1.0",
            timestamp=datetime.now(timezone.utc),
        )

    async def get_readiness_status(self) -> tuple[int, ReadinessResponse]:
        db_healthy = await check_db_health()
        storage_healthy = await self.storage.check_health()

        db_state = "connected" if db_healthy else "disconnected"
        storage_state = "connected" if storage_healthy else "disconnected"

        is_all_ok = db_healthy and storage_healthy
        status_text = "ok" if is_all_ok else "degraded"
        status_code = 200 if is_all_ok else 503

        return status_code, ReadinessResponse(
            status=status_text,
            database=db_state,
            storage=storage_state,
            timestamp=datetime.now(timezone.utc),
        )
