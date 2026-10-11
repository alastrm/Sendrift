import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings
from app.repositories.share_repository import ShareRepository
from app.schemas.share import TTL_SECONDS_MAP, ShareCreateResponse
from app.storage.s3 import (
    EmptyFileError,
    FileSizeLimitExceededError,
    S3StorageAdapter,
    StorageError,
    StorageUnavailableError,
    get_storage,
)

logger = logging.getLogger(__name__)


def generate_tokens() -> tuple[str, str, str, str]:
    """Generate independent >=256-bit cryptographic secrets and their SHA-256 hashes."""
    share_token = secrets.token_urlsafe(32)
    manage_token = secrets.token_urlsafe(32)
    share_token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
    manage_token_hash = hashlib.sha256(manage_token.encode("utf-8")).hexdigest()
    return share_token, manage_token, share_token_hash, manage_token_hash


def generate_object_key() -> str:
    """Generate an unpredictable random object key in the private bucket."""
    return secrets.token_hex(16)


def sanitize_filename(raw_filename: str | None) -> str:
    """Extract clean basename, removing path traversal sequences and null bytes."""
    if not raw_filename:
        return "unnamed_file"
    normalized = raw_filename.replace("\\", "/")
    clean = os.path.basename(normalized).strip().replace("\x00", "")
    return clean or "unnamed_file"


class ShareService:
    """Business service orchestrating file upload, verification, and share lifecycle."""

    def __init__(
        self,
        share_repo: ShareRepository,
        storage: S3StorageAdapter | None = None,
    ) -> None:
        self.share_repo = share_repo
        self.storage = storage or get_storage()

    async def create_share(
        self,
        file: UploadFile,
        ttl_str: str,
        base_url: str,
    ) -> ShareCreateResponse:
        """Upload file, stream to private storage, and transition share to ready."""
        if ttl_str not in TTL_SECONDS_MAP:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid TTL '{ttl_str}'. Allowed values: 1h, 24h, 7d",
            )
        ttl_seconds = TTL_SECONDS_MAP[ttl_str]  # type: ignore[index]

        share_token, manage_token, share_hash, manage_hash = generate_tokens()
        object_key = generate_object_key()
        filename = sanitize_filename(file.filename)
        content_type = file.content_type or "application/octet-stream"

        # 1. Create database record in 'uploading' state
        try:
            share = await self.share_repo.create_uploading(
                share_token_hash=share_hash,
                manage_token_hash=manage_hash,
                object_key=object_key,
                original_filename=filename,
                ttl_seconds=ttl_seconds,
                content_type=content_type,
            )
        except Exception as exc:
            logger.error("Failed to initialize share record in DB: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to initiate upload record",
            ) from exc

        # 2. Stream upload to storage with bounded size reader
        try:
            bytes_uploaded = await self.storage.put_stream(
                object_key=object_key,
                stream=file.file,
                max_size_bytes=settings.MAX_FILE_SIZE_BYTES,
                content_type="application/octet-stream",
            )
        except EmptyFileError:
            await self.share_repo.mark_failed(share.id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is empty (0 bytes)",
            ) from None
        except FileSizeLimitExceededError:
            await self.share_repo.mark_failed(share.id)
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="File size exceeds maximum allowed limit of 100 MiB",
            ) from None
        except (StorageUnavailableError, StorageError) as exc:
            await self.share_repo.mark_failed(share.id)
            logger.error("Storage failure during upload: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Storage service temporarily unavailable",
            ) from exc
        except Exception as exc:
            await self.share_repo.mark_failed(share.id)
            await self.storage.delete_object(object_key)
            logger.error("Unexpected failure during upload: %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process file upload",
            ) from exc

        # 3. Transition to 'ready' and record exact server UTC expiration
        now_utc = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(seconds=ttl_seconds)

        try:
            ready_share = await self.share_repo.mark_ready(
                share_id=share.id,
                size_bytes=bytes_uploaded,
                ready_at=now_utc,
                expires_at=expires_at,
            )
            if not ready_share:
                raise RuntimeError("Failed to transition share to ready")
        except Exception as exc:
            logger.error("DB commit failed finalizing share %s: %s", share.id, type(exc).__name__)
            await self.storage.delete_object(object_key)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to finalize share record",
            ) from exc

        # 4. Return success response contract
        clean_base = base_url.rstrip("/")
        share_url = f"{clean_base}/s/{share_token}"

        return ShareCreateResponse(
            share_url=share_url,
            manage_token=manage_token,
            filename=filename,
            size_bytes=bytes_uploaded,
            expires_at=expires_at,
        )
