from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness health check response."""
    status: Literal["ok", "degraded", "error"] = Field(
        ..., description="Overall liveness status of the API"
    )
    app: str = Field(..., description="Application name")
    version: str = Field(..., description="API version")
    timestamp: datetime = Field(..., description="Current UTC timestamp")


class ReadinessResponse(BaseModel):
    """Readiness health check response evaluating essential infrastructure."""
    status: Literal["ok", "degraded", "error"] = Field(
        ..., description="Readiness status of dependencies"
    )
    database: Literal["connected", "disconnected"] = Field(
        ..., description="PostgreSQL connectivity state"
    )
    storage: Literal["connected", "disconnected"] = Field(
        ..., description="S3/MinIO private bucket reachability state"
    )
    timestamp: datetime = Field(..., description="Current UTC timestamp")
