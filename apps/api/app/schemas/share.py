from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TTLOption = Literal["1h", "24h", "7d"]

TTL_SECONDS_MAP: dict[TTLOption, int] = {
    "1h": 3600,
    "24h": 86400,
    "7d": 604800,
}


class ShareCreateResponse(BaseModel):
    """Response contract for successful file upload (POST /api/v1/shares)."""

    share_url: str = Field(
        ...,
        description="Public URL for recipients to access metadata and download the file",
    )
    manage_token: str = Field(
        ...,
        description="High-entropy secret token issued once to the sender for early deletion",
    )
    filename: str = Field(
        ...,
        description="Sanitized original filename as metadata",
    )
    size_bytes: int = Field(
        ...,
        ge=1,
        le=104857600,
        description="Verified file size in bytes (1 to 104857600)",
    )
    expires_at: datetime = Field(
        ...,
        description="Expiration time in server UTC (ready_at + ttl)",
    )
