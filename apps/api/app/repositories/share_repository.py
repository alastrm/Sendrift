import uuid
from datetime import datetime, timezone

from sqlalchemy import and_, delete, func, or_, select, update
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
            .where(Share.id == share_id, Share.status == ShareStatus.UPLOADING)
            .values(
                status=ShareStatus.READY,
                size_bytes=size_bytes,
                ready_at=ready_at,
                expires_at=expires_at,
                updated_at=func.clock_timestamp(),
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
            .where(Share.id == share_id, Share.status == ShareStatus.UPLOADING)
            .values(status=ShareStatus.FAILED, updated_at=func.clock_timestamp())
            .returning(Share)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().first()

    async def get_by_share_token_hash(self, token_hash: str) -> Share | None:
        """Find share by SHA-256 hash of public share token."""
        stmt = (
            select(Share).where(Share.share_token_hash == token_hash)
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def lock_uploading(self, share_id: uuid.UUID) -> Share | None:
        """Hold the upload row lock until finalization so cleanup skips active uploads."""
        stmt = select(Share).where(
            Share.id == share_id, Share.status == ShareStatus.UPLOADING,
        ).with_for_update()
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def lock_deleting(self, share_id: uuid.UUID) -> bool:
        """Acquire exclusive deletion ownership after the durable DELETING commit."""
        stmt = select(Share.id).where(
            Share.id == share_id, Share.status == ShareStatus.DELETING,
        ).with_for_update(skip_locked=True)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def increment_download_count(self, share_id: uuid.UUID) -> None:
        """Atomically increment the download_count when a transfer is initiated."""
        stmt = (
            update(Share)
            .where(
                Share.id == share_id, Share.status == ShareStatus.READY,
                Share.expires_at > func.clock_timestamp(),
            )
            .values(download_count=Share.download_count + 1)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def mark_deleting(self, share_id: uuid.UUID) -> Share | None:
        """Transition share to deleting status and update timestamp."""
        stmt = (
            update(Share)
            .where(Share.id == share_id)
            .values(
                status=ShareStatus.DELETING,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(Share)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalars().first()

    async def delete_share_record(self, share_id: uuid.UUID) -> bool:
        """Permanently delete a share record from database."""
        stmt = delete(Share).where(Share.id == share_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return bool(result.rowcount and result.rowcount > 0)

    async def get_existing_object_keys(self, object_keys: list[str]) -> set[str]:
        """Return set of object keys that exist in the database."""
        if not object_keys:
            return set()
        stmt = select(Share.object_key).where(Share.object_key.in_(object_keys))
        result = await self.session.execute(stmt)
        return set(result.scalars().all())

    async def fetch_and_lock_next_candidate(
        self,
        now_utc: datetime,
        stale_upload_cutoff: datetime,
        stale_failed_cutoff: datetime,
        exclude_ids: set[uuid.UUID] | None = None,
    ) -> Share | None:
        """Fetch and lock a single candidate for cleanup using FOR UPDATE SKIP LOCKED."""
        conditions = [
            or_(
                # 1. Expired ready shares
                and_(
                    Share.status == ShareStatus.READY,
                    Share.expires_at <= now_utc,
                ),
                # 2. Shares already in deleting status
                Share.status == ShareStatus.DELETING,
                # 3. Failed shares past grace period
                and_(
                    Share.status == ShareStatus.FAILED,
                    Share.updated_at <= stale_failed_cutoff,
                ),
                # 4. Stale uploading shares past upload timeout
                and_(
                    Share.status == ShareStatus.UPLOADING,
                    Share.updated_at <= stale_upload_cutoff,
                ),
            )
        ]
        if exclude_ids:
            conditions.append(Share.id.not_in(exclude_ids))

        stmt = (
            select(Share)
            .where(*conditions)
            .order_by(Share.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()
