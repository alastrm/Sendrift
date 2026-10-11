import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.share import Share, ShareStatus
from app.repositories.base import BaseRepository


class ShareRepository(BaseRepository[Share]):
    """Repository handling database operations for Shares."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Share, session)

    async def create_uploading(
        self,
        share_token_hash: str,
        manage_token_hash: str,
        object_key: str,
        original_filename: str,
        ttl_seconds: int,
        content_type: str | None = None,
    ) -> Share:
        """Create a new Share in uploading status and persist to database."""
        share = Share(
            id=uuid.uuid4(),
            share_token_hash=share_token_hash,
            manage_token_hash=manage_token_hash,
            object_key=object_key,
            original_filename=original_filename,
            content_type=content_type,
            ttl_seconds=ttl_seconds,
            status=ShareStatus.UPLOADING,
        )
        self.session.add(share)
        await self.session.commit()
        await self.session.refresh(share)
        return share

    async def mark_ready(
        self,
        share_id: uuid.UUID,
        size_bytes: int,
        ready_at: datetime,
        expires_at: datetime,
    ) -> Share | None:
        """Transition share from uploading to ready and record expiration."""
        stmt = (
            update(Share)
            .where(Share.id == share_id)
            .values(
                status=ShareStatus.READY,
                size_bytes=size_bytes,
                ready_at=ready_at,
                expires_at=expires_at,
            )
            .returning(Share)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().first()

    async def mark_failed(self, share_id: uuid.UUID) -> Share | None:
        """Mark share as failed on interrupted/rejected upload."""
        stmt = (
            update(Share)
            .where(Share.id == share_id)
            .values(status=ShareStatus.FAILED)
            .returning(Share)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().first()

    async def get_by_share_token_hash(self, token_hash: str) -> Share | None:
        """Find share by SHA-256 hash of public share token."""
        stmt = select(Share).where(Share.share_token_hash == token_hash)
        result = await self.session.execute(stmt)
        return result.scalars().first()
