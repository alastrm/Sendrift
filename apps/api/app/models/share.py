import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ShareStatus(str, enum.Enum):
    UPLOADING = "uploading"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class Share(Base):
    __tablename__ = "shares"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    share_token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    manage_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )
    object_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    original_filename: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    content_type: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    size_bytes: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )
    ttl_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    status: Mapped[ShareStatus] = mapped_column(
        Enum(ShareStatus, name="share_status"),
        nullable=False,
        default=ShareStatus.UPLOADING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    download_count: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "ttl_seconds IN (3600, 86400, 604800)",
            name="ck_shares_ttl_seconds",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR (size_bytes >= 1 AND size_bytes <= 104857600)",
            name="ck_shares_size_bytes",
        ),
    )
