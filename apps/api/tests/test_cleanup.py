import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.share import Share, ShareStatus
from app.repositories.share_repository import ShareRepository
from app.services.cleanup_service import CleanupResult, CleanupService
from app.storage.s3 import S3StorageAdapter
from app.worker.cleanup import run_worker_loop


def create_fake_share(
    status: ShareStatus = ShareStatus.READY,
    minutes_ago_created: int = 10,
    minutes_until_expiry: int = -5,  # negative means expired
    minutes_ago_updated: int = 10,
) -> Share:
    now_utc = datetime.now(timezone.utc)
    return Share(
        id=uuid.uuid4(),
        share_token_hash="fake-share-hash-" + uuid.uuid4().hex,
        manage_token_hash="fake-manage-hash-" + uuid.uuid4().hex,
        object_key="fake-obj-key-" + uuid.uuid4().hex,
        original_filename="document.pdf",
        content_type="application/pdf",
        ttl_seconds=3600,
        status=status,
        size_bytes=1024,
        download_count=2,
        created_at=now_utc - timedelta(minutes=minutes_ago_created),
        ready_at=now_utc - timedelta(minutes=minutes_ago_created),
        expires_at=now_utc + timedelta(minutes=minutes_until_expiry),
        updated_at=now_utc - timedelta(minutes=minutes_ago_updated),
    )


@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=S3StorageAdapter)
    storage.delete_object = AsyncMock(return_value=True)
    storage.list_object_keys = AsyncMock(return_value=[])
    async def empty_batches(**kwargs):
        for keys in []:
            yield keys
    storage.iter_object_key_batches = empty_batches
    return storage


@pytest.mark.asyncio
async def test_cleanup_expired_ready_share(mock_storage):
    """Expired ready shares are deleted from S3 and database."""
    expired_share = create_fake_share(
        status=ShareStatus.READY,
        minutes_until_expiry=-5,
    )

    # First call returns expired_share, second call returns None
    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(
        side_effect=[expired_share, None]
    )
    mock_repo.delete_share_record = AsyncMock(return_value=True)

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    # Patch ShareRepository in cleanup_service to return our mock_repo
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result: CleanupResult = await service.run_cleanup_cycle()

    assert result.deleted_db_rows == 1
    assert result.deleted_s3_objects == 1
    assert result.expired_ready_cleaned == 1
    assert result.storage_errors == 0
    assert result.db_errors == 0

    mock_storage.delete_object.assert_awaited_once_with(expired_share.object_key)
    mock_repo.delete_share_record.assert_awaited_once_with(expired_share.id)


@pytest.mark.asyncio
async def test_cleanup_preserves_unexpired_share(mock_storage):
    """Unexpired shares are not selected and not deleted."""
    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(return_value=None)
    mock_repo.delete_share_record = AsyncMock()

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle()

    assert result.total_cleaned == 0
    assert result.deleted_s3_objects == 0
    mock_storage.delete_object.assert_not_called()
    mock_repo.delete_share_record.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_stale_uploading_share(mock_storage):
    """Abandoned uploads older than stale cutoff are deleted."""
    stale_upload = create_fake_share(
        status=ShareStatus.UPLOADING,
        minutes_ago_created=120,  # 2 hours old
    )

    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(
        side_effect=[stale_upload, None]
    )
    mock_repo.delete_share_record = AsyncMock(return_value=True)

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
        stale_upload_seconds=3600,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle()

    assert result.deleted_db_rows == 1
    assert result.stale_uploading_cleaned == 1
    assert result.deleted_s3_objects == 1
    mock_storage.delete_object.assert_awaited_once_with(stale_upload.object_key)
    mock_repo.delete_share_record.assert_awaited_once_with(stale_upload.id)


@pytest.mark.asyncio
async def test_cleanup_deleting_status_share(mock_storage):
    """Owner-revoked shares in DELETING status are cleaned up."""
    deleting_share = create_fake_share(
        status=ShareStatus.DELETING,
        minutes_ago_created=5,
    )

    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(
        side_effect=[deleting_share, None]
    )
    mock_repo.delete_share_record = AsyncMock(return_value=True)

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle()

    assert result.deleted_db_rows == 1
    assert result.owner_revoked_cleaned == 1
    assert result.deleted_s3_objects == 1
    mock_storage.delete_object.assert_awaited_once_with(deleting_share.object_key)
    mock_repo.delete_share_record.assert_awaited_once_with(deleting_share.id)


@pytest.mark.asyncio
async def test_cleanup_storage_failure_preserves_db_row(mock_storage):
    """When S3 deletion fails, DB row is NOT deleted and kept for retry."""
    expired_share = create_fake_share(status=ShareStatus.READY, minutes_until_expiry=-10)

    # Storage delete returns False (simulating S3 failure)
    mock_storage.delete_object = AsyncMock(return_value=False)

    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(side_effect=[expired_share, None])
    mock_repo.delete_share_record = AsyncMock()

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle()

    assert result.storage_errors == 1
    assert result.deleted_s3_objects == 0
    assert result.deleted_db_rows == 0
    # DB row must NOT be deleted if S3 deletion failed!
    mock_repo.delete_share_record.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_db_failure_handled(mock_storage):
    """When DB row delete raises an error, S3 is deleted but error is counted."""
    expired_share = create_fake_share(status=ShareStatus.READY, minutes_until_expiry=-10)

    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(return_value=expired_share)
    mock_repo.delete_share_record = AsyncMock(side_effect=RuntimeError("DB disconnect"))

    session = MagicMock()
    session.commit = AsyncMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle()

    assert result.deleted_s3_objects == 1
    assert result.deleted_db_rows == 0
    assert result.db_errors == 1


@pytest.mark.asyncio
async def test_cleanup_orphan_s3_reconciliation(mock_storage):
    """Audit pass discovers and removes orphan objects absent from DB."""
    s3_orphan_key = "orphan-key-999"
    s3_known_key = "known-key-111"

    async def batches(**kwargs):
        yield [s3_orphan_key, s3_known_key]
    mock_storage.iter_object_key_batches = batches
    mock_storage.delete_object = AsyncMock(return_value=True)

    mock_repo = MagicMock(spec=ShareRepository)
    mock_repo.fetch_and_lock_next_candidate = AsyncMock(return_value=None)
    mock_repo.get_existing_object_keys = AsyncMock(side_effect=[{s3_known_key}, set()])

    session = MagicMock()

    class FakeSessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    mock_session_factory = MagicMock(return_value=FakeSessionContext())

    service = CleanupService(
        session_factory=mock_session_factory,
        storage=mock_storage,
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.services.cleanup_service.ShareRepository",
            lambda sess: mock_repo,
        )
        result = await service.run_cleanup_cycle(reconcile_orphans=True)

    assert result.orphan_s3_objects_cleaned == 1
    mock_storage.delete_object.assert_awaited_once_with(s3_orphan_key)


@pytest.mark.asyncio
async def test_worker_loop_graceful_shutdown():
    """Worker loop stops cleanly when stop_event is signaled."""
    stop_event = asyncio.Event()

    # Signal stop after 50ms
    async def trigger_stop():
        await asyncio.sleep(0.05)
        stop_event.set()

    asyncio.create_task(trigger_stop())

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.worker.cleanup.run_cleanup_cycle",
            AsyncMock(return_value=CleanupResult()),
        )
        # Should exit within ~100ms when stop_event is set
        await run_worker_loop(interval_seconds=10, stop_event=stop_event)

    assert stop_event.is_set()
