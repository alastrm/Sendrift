"""Controlled cleanup races in a private bucket and a disposable PostgreSQL schema."""

import asyncio
import importlib
import io
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import UploadFile
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.share import Share, ShareStatus
from app.repositories.share_repository import ShareRepository
from app.services.cleanup_service import CleanupService
from app.services.share_service import ShareService, hash_token
from app.storage.s3 import S3StorageAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("SENDRIFT_REAL_INTEGRATION") != "1",
    reason="Set SENDRIFT_REAL_INTEGRATION=1 for disposable PostgreSQL/MinIO tests",
)


@pytest.fixture
async def isolated_cleanup():
    identifier = uuid.uuid4().hex
    schema = f"cleanup_test_{identifier}"
    admin = create_engine(settings.sync_database_url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        with Operations.context(MigrationContext.configure(connection)):
            importlib.import_module("migrations.versions.0001_create_shares_table").upgrade()
    engine = create_async_engine(
        settings.async_database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}, "timeout": 5},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    storage = S3StorageAdapter(bucket_name=f"cleanup-test-{identifier}")
    try:
        await storage.ensure_bucket()
        yield factory, storage
    finally:
        for key in await storage.list_object_keys():
            await storage.delete_object(key)
        storage._s3_client.delete_bucket(Bucket=storage.bucket_name)
        storage._s3_client.close()
        await engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


async def seed(factory, storage, status=ShareStatus.READY, recently_updated=False):
    now = datetime.now(timezone.utc)
    key = uuid.uuid4().hex
    async with factory() as session:
        share = Share(
            share_token_hash=hash_token(key), manage_token_hash=hash_token(f"manage-{key}"),
            object_key=key, original_filename="probe.bin", ttl_seconds=3600,
            status=status, size_bytes=1, ready_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1), created_at=now - timedelta(hours=2),
            updated_at=now if recently_updated else now - timedelta(hours=2),
        )
        session.add(share)
        await session.commit()
    await storage.put_stream(key, io.BytesIO(b"x"), 1)
    return share


@pytest.mark.asyncio
async def test_worker_keeps_exclusive_ownership_during_s3_delete(isolated_cleanup, monkeypatch):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    original_delete = storage.delete_object

    async def gated_delete(key):
        calls.append(key)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return await original_delete(key)

    monkeypatch.setattr(storage, "delete_object", gated_delete)
    first = asyncio.create_task(CleanupService(factory, storage).run_cleanup_cycle())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = await asyncio.wait_for(CleanupService(factory, storage).run_cleanup_cycle(), 5)
        assert second.total_cleaned == 0
        assert calls == [share.object_key]
    finally:
        release.set()
        await first


@pytest.mark.asyncio
async def test_slow_active_upload_is_not_cleaned_as_stale(isolated_cleanup, monkeypatch):
    factory, storage = isolated_cleanup
    entered = asyncio.Event()
    release = asyncio.Event()
    original_put = storage.put_stream
    original_create = ShareRepository.create_uploading

    async def old_create(repo, *args, **kwargs):
        share = await original_create(repo, *args, **kwargs)
        await repo.session.execute(update(Share).where(Share.id == share.id).values(
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        await repo.session.commit()
        return share

    async def slow_put(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_put(*args, **kwargs)

    monkeypatch.setattr(ShareRepository, "create_uploading", old_create)
    monkeypatch.setattr(storage, "put_stream", slow_put)

    async def upload():
        async with factory() as session:
            return await ShareService(ShareRepository(session), storage).create_share(
                UploadFile(io.BytesIO(b"x"), filename="probe.bin"), "1h", "http://test",
            )

    task = asyncio.create_task(upload())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await CleanupService(factory, storage).run_cleanup_cycle()
        assert result.total_cleaned == 0
        release.set()
        response = await asyncio.wait_for(task, 5)
        assert response.size_bytes == 1
        async with factory() as session:
            share = (await session.execute(select(Share))).scalar_one()
            assert share.status == ShareStatus.READY
            assert storage._s3_client.head_object(
                Bucket=storage.bucket_name, Key=share.object_key,
            )["ContentLength"] == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ShareStatus.DELETING, ShareStatus.FAILED])
async def test_upload_finalization_cannot_resurrect_claimed_rows(isolated_cleanup, status):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage, status=status)
    async with factory() as session:
        now = datetime.now(timezone.utc)
        result = await ShareRepository(session).mark_ready(
            share.id, 1, now, now + timedelta(hours=1),
        )
        assert result is None
    async with factory() as session:
        assert (await session.get(Share, share.id)).status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [ShareStatus.UPLOADING, ShareStatus.FAILED])
async def test_staleness_uses_last_activity_instead_of_creation(isolated_cleanup, status):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage, status=status, recently_updated=True)
    result = await CleanupService(factory, storage).run_cleanup_cycle()
    assert result.total_cleaned == 0
    async with factory() as session:
        assert await session.get(Share, share.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
async def test_reconciliation_preserves_recent_untracked_objects(isolated_cleanup, multipart):
    factory, storage = isolated_cleanup
    key = uuid.uuid4().hex
    client = storage._s3_client
    if multipart:
        upload_id = client.create_multipart_upload(Bucket=storage.bucket_name, Key=key)["UploadId"]
    else:
        await storage.put_stream(key, io.BytesIO(b"x"), 1)
    try:
        result = await CleanupService(factory, storage).run_cleanup_cycle(reconcile_orphans=True)
        assert result.orphan_s3_objects_cleaned == 0
        if multipart:
            uploads = client.list_multipart_uploads(Bucket=storage.bucket_name)["Uploads"]
            assert any(upload["UploadId"] == upload_id for upload in uploads)
        else:
            assert client.head_object(Bucket=storage.bucket_name, Key=key)["ContentLength"] == 1
    finally:
        if multipart:
            client.abort_multipart_upload(Bucket=storage.bucket_name, Key=key, UploadId=upload_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("head", [False, True])
@pytest.mark.parametrize("cause", ["revocation", "expiry"])
async def test_access_loss_during_s3_open_blocks_download(
    isolated_cleanup, monkeypatch, head, cause,
):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage)
    async with factory() as session:
        await session.execute(update(Share).where(Share.id == share.id).values(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        await session.commit()
    original_get = storage.get_stream
    streams = []

    async def revoking_get(key):
        stream = await original_get(key)
        streams.append(stream)
        async with factory() as session:
            if cause == "revocation":
                await ShareRepository(session).mark_deleting(share.id)
            else:
                await session.execute(update(Share).where(Share.id == share.id).values(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                ))
                await session.commit()
        return stream

    monkeypatch.setattr(storage, "get_stream", revoking_get)
    from fastapi import HTTPException

    try:
        async with factory() as session:
            with pytest.raises(HTTPException) as denied:
                await ShareService(ShareRepository(session), storage).get_share_for_download(
                    share.object_key, is_head_request=head,
                )
            assert denied.value.status_code == 404
        assert streams[0]._closed
        async with factory() as session:
            assert (await session.get(Share, share.id)).download_count == 0
    finally:
        for stream in streams:
            await stream.close()


@pytest.mark.asyncio
async def test_crash_after_s3_delete_leaves_retryable_row(isolated_cleanup, monkeypatch):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage)
    original_delete = storage.delete_object

    async def crash_after_delete(key):
        assert await original_delete(key)
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_object", crash_after_delete)
        with pytest.raises(asyncio.CancelledError):
            await CleanupService(factory, storage).run_cleanup_cycle()
    async with factory() as session:
        assert (await session.get(Share, share.id)).status == ShareStatus.DELETING
    recovered = await CleanupService(factory, storage).run_cleanup_cycle()
    assert recovered.total_cleaned == 1


@pytest.mark.asyncio
async def test_failed_upload_timestamp_is_failure_time_not_transaction_start(isolated_cleanup):
    factory, storage = isolated_cleanup
    share = await seed(factory, storage, status=ShareStatus.UPLOADING)
    async with factory() as session:
        repo = ShareRepository(session)
        await repo.lock_uploading(share.id)
        failure_time = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
        failed = await repo.mark_failed(share.id)
        assert failed.updated_at >= failure_time
    assert (await CleanupService(factory, storage).run_cleanup_cycle()).total_cleaned == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_commit", [1, 2])
async def test_cleanup_commit_failure_recovery(isolated_cleanup, monkeypatch, failed_commit):
    from sqlalchemy.ext.asyncio import AsyncSession

    factory, storage = isolated_cleanup
    share = await seed(factory, storage)
    original_commit = AsyncSession.commit
    commits = 0

    async def failing_commit(session):
        nonlocal commits
        commits += 1
        if commits == failed_commit:
            raise RuntimeError("injected commit failure")
        await original_commit(session)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", failing_commit)
        result = await CleanupService(factory, storage).run_cleanup_cycle()
    assert result.db_errors == 1
    assert result.total_cleaned == 0
    async with factory() as session:
        row = await session.get(Share, share.id)
        assert row is not None
        assert row.status == (ShareStatus.READY if failed_commit == 1 else ShareStatus.DELETING)
    if failed_commit == 1:
        assert storage._s3_client.head_object(
            Bucket=storage.bucket_name, Key=share.object_key,
        )["ContentLength"] == 1
    recovered = await CleanupService(factory, storage).run_cleanup_cycle()
    assert recovered.total_cleaned == 1


@pytest.mark.asyncio
async def test_one_undeletable_object_does_not_starve_other_cleanup(isolated_cleanup, monkeypatch):
    factory, storage = isolated_cleanup
    blocked = await seed(factory, storage)
    healthy = await seed(factory, storage)
    original_delete = storage.delete_object

    async def selective_delete(key):
        if key == blocked.object_key:
            return False
        return await original_delete(key)

    with monkeypatch.context() as patch:
        patch.setattr(storage, "delete_object", selective_delete)
        result = await CleanupService(factory, storage).run_cleanup_cycle()
    assert result.storage_errors == 1
    assert result.total_cleaned == 1
    async with factory() as session:
        assert (await session.get(Share, blocked.id)).status == ShareStatus.DELETING
        assert await session.get(Share, healthy.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tracked", [True, False])
async def test_crashed_multipart_upload_parts_are_removed(isolated_cleanup, tracked):
    factory, storage = isolated_cleanup
    if tracked:
        share = await seed(factory, storage, status=ShareStatus.UPLOADING)
        key = share.object_key
        await storage.delete_object(key)
    else:
        key = uuid.uuid4().hex
    client = storage._s3_client
    upload_id = client.create_multipart_upload(Bucket=storage.bucket_name, Key=key)["UploadId"]
    try:
        client.upload_part(
            Bucket=storage.bucket_name, Key=key, UploadId=upload_id, PartNumber=1, Body=b"x" * 1024,
        )
        service = CleanupService(factory, storage, orphan_grace_seconds=0)
        await service.run_cleanup_cycle(reconcile_orphans=not tracked)
        uploads = client.list_multipart_uploads(Bucket=storage.bucket_name).get("Uploads", [])
        assert all(upload["UploadId"] != upload_id for upload in uploads)
    finally:
        # Keep the disposable bucket removable even when testing the old implementation.
        uploads = client.list_multipart_uploads(Bucket=storage.bucket_name).get("Uploads", [])
        if any(upload["UploadId"] == upload_id for upload in uploads):
            client.abort_multipart_upload(Bucket=storage.bucket_name, Key=key, UploadId=upload_id)
