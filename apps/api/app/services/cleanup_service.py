import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.share import ShareStatus
from app.repositories.share_repository import ShareRepository
from app.storage.s3 import S3StorageAdapter, get_storage

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    """Summary statistics for a completed cleanup cycle."""

    expired_ready_cleaned: int = 0
    stale_uploading_cleaned: int = 0
    stale_failed_cleaned: int = 0
    owner_revoked_cleaned: int = 0
    deleted_s3_objects: int = 0
    deleted_db_rows: int = 0
    orphan_s3_objects_cleaned: int = 0
    storage_errors: int = 0
    db_errors: int = 0

    @property
    def total_cleaned(self) -> int:
        return self.deleted_db_rows


class CleanupService:
    """Service orchestrating background expiration, crash recovery, and object cleanup."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        storage: S3StorageAdapter | None = None,
        stale_upload_seconds: int = settings.CLEANUP_STALE_UPLOAD_SECONDS,
        stale_failed_seconds: int = settings.CLEANUP_STALE_FAILED_SECONDS,
        orphan_grace_seconds: int = settings.CLEANUP_STALE_UPLOAD_SECONDS,
    ) -> None:
        self.session_factory = session_factory or AsyncSessionLocal
        self.storage = storage or get_storage()
        self.stale_upload_seconds = stale_upload_seconds
        self.stale_failed_seconds = stale_failed_seconds
        self.orphan_grace_seconds = orphan_grace_seconds

    async def run_cleanup_cycle(
        self,
        max_items: int = settings.CLEANUP_BATCH_SIZE,
        reconcile_orphans: bool = False,
    ) -> CleanupResult:
        """Execute one complete pass of candidate cleanup and optional reconciliation."""
        result = CleanupResult()
        now_utc = datetime.now(timezone.utc)
        stale_upload_cutoff = now_utc - timedelta(seconds=self.stale_upload_seconds)
        stale_failed_cutoff = now_utc - timedelta(seconds=self.stale_failed_seconds)

        exclude_ids: set[uuid.UUID] = set()

        for _ in range(max_items):
            try:
                async with self.session_factory() as session:
                    repo = ShareRepository(session)
                    candidate = await repo.fetch_and_lock_next_candidate(
                        now_utc=now_utc,
                        stale_upload_cutoff=stale_upload_cutoff,
                        stale_failed_cutoff=stale_failed_cutoff,
                        exclude_ids=exclude_ids,
                    )
                    if not candidate:
                        break

                    candidate_id = candidate.id
                    candidate_key = candidate.object_key
                    prev_status = candidate.status

                    if candidate.status != ShareStatus.DELETING:
                        candidate.status = ShareStatus.DELETING
                        candidate.updated_at = now_utc
                        await session.commit()
                        # The commit releases FOR UPDATE. Reacquire ownership
                        # before S3; another worker may already be processing it.
                        if not await repo.lock_deleting(candidate_id):
                            exclude_ids.add(candidate_id)
                            continue

                    # Keep the row locked across S3 deletion and DB removal.
                    # A crash releases the lock and leaves DELETING for retry.
                    try:
                        s3_deleted = await self.storage.delete_object(candidate_key)
                    except Exception as exc:
                        logger.warning("Storage deletion failed: %s", type(exc).__name__)
                        s3_deleted = False
                    if not s3_deleted:
                        result.storage_errors += 1
                        exclude_ids.add(candidate_id)
                        continue

                    result.deleted_s3_objects += 1
                    deleted = await repo.delete_share_record(candidate_id)
                    if deleted:
                        result.deleted_db_rows += 1
                        if prev_status == ShareStatus.READY:
                            result.expired_ready_cleaned += 1
                        elif prev_status == ShareStatus.UPLOADING:
                            result.stale_uploading_cleaned += 1
                        elif prev_status == ShareStatus.FAILED:
                            result.stale_failed_cleaned += 1
                        elif prev_status == ShareStatus.DELETING:
                            result.owner_revoked_cleaned += 1
            except Exception as exc:
                result.db_errors += 1
                logger.error("Database cleanup failed: %s", type(exc).__name__)
                break

        # 4. Optional reconciliation for orphan S3 objects missing database records
        if reconcile_orphans:
            await self._reconcile_orphan_objects(result)

        logger.info(
            "Cleanup cycle summary: %d rows removed "
            "(%d ready, %d deleting, %d stale uploads, %d failed), "
            "%d S3 objects deleted, %d orphan objects, %d storage errors, %d db errors",
            result.deleted_db_rows,
            result.expired_ready_cleaned,
            result.owner_revoked_cleaned,
            result.stale_uploading_cleaned,
            result.stale_failed_cleaned,
            result.deleted_s3_objects,
            result.orphan_s3_objects_cleaned,
            result.storage_errors,
            result.db_errors,
        )
        return result

    async def _reconcile_orphan_objects(self, result: CleanupResult) -> None:
        """Scan S3 bucket and delete orphan objects that have no matching DB share records."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.orphan_grace_seconds)
        try:
            # Traverse all pages with bounded memory; a fixed first-1000 scan
            # would permanently miss orphans behind 1000 live objects.
            async for s3_keys in self.storage.iter_object_key_batches(older_than=cutoff):
                async with self.session_factory() as session:
                    known_keys = await ShareRepository(session).get_existing_object_keys(s3_keys)
                for orphan_key in set(s3_keys) - known_keys:
                    # Do not act solely on the earlier batch snapshot.
                    async with self.session_factory() as session:
                        existing = await ShareRepository(session).get_existing_object_keys(
                            [orphan_key],
                        )
                    if existing:
                        continue
                    if await self.storage.delete_object(orphan_key):
                        result.orphan_s3_objects_cleaned += 1
                    else:
                        result.storage_errors += 1
        except Exception as exc:
            logger.warning("Error during orphan object reconciliation: %s", type(exc).__name__)
            result.storage_errors += 1
