"""Real integration tests for File Lifecycle and Cleanup Worker against PostgreSQL and MinIO."""
import asyncio
import hashlib
import os
import secrets
import uuid
from unittest.mock import AsyncMock

import boto3
import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.share import Share
from app.repositories.share_repository import ShareRepository
from app.services.cleanup_service import CleanupResult, CleanupService
from app.storage.s3 import S3StorageAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("SENDRIFT_REAL_INTEGRATION") != "1",
    reason="Set SENDRIFT_REAL_INTEGRATION=1 to run real cleanup integration tests",
)


@pytest.fixture
def test_engine():
    return create_async_engine(settings.async_database_url, poolclass=NullPool)


@pytest.fixture
def session_factory(test_engine):
    return async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


@pytest.fixture(autouse=True)
async def cleanup_harness(monkeypatch, session_factory, s3_client, test_engine):
    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[get_db] = _override_get_db
    keys: list[str] = []
    original_create = ShareRepository.create_uploading

    async def tracking_create(repository, *args, **kwargs):
        keys.append(kwargs["object_key"])
        return await original_create(repository, *args, **kwargs)

    monkeypatch.setattr(ShareRepository, "create_uploading", tracking_create)
    try:
        yield keys
    finally:
        app.dependency_overrides.pop(get_db, None)
        for key in keys:
            try:
                s3_client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
            except Exception:
                pass
        async with session_factory() as session:
            await session.execute(delete(Share).where(Share.object_key.in_(keys)))
            await session.commit()
        await test_engine.dispose()


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        dbname=settings.POSTGRES_DB,
        connect_timeout=5,
    )
    yield conn
    conn.close()


@pytest.fixture
def s3_client():
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name=settings.S3_REGION,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=5,
        ),
    )


# ---------------------------------------------------------------------------
# Real Integration Tests: File Lifecycle & Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_expired_file_cleanup(session_factory, s3_client, db_conn, cleanup_harness):
    """Real upload, premature expiration in DB, and complete cleanup from MinIO and Postgres."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        payload = b"Payload for expired cleanup test: " + secrets.token_bytes(64)
        files = {"file": ("expired_target.txt", payload, "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201

        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        # Find object key from DB
        cur = db_conn.cursor()
        cur.execute("SELECT object_key FROM shares WHERE share_token_hash = %s", (token_hash,))
        object_key = cur.fetchone()[0]

        # Verify object initially exists in MinIO
        head_resp = s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        assert head_resp["ResponseMetadata"]["HTTPStatusCode"] == 200

        # Mark share as expired in real PostgreSQL
        cur.execute(
            "UPDATE shares SET expires_at = NOW() - INTERVAL '1 minute' "
            "WHERE share_token_hash = %s",
            (token_hash,),
        )
        db_conn.commit()
        cur.close()

        # Run CleanupService cycle
        service = CleanupService(session_factory=session_factory)
        result: CleanupResult = await service.run_cleanup_cycle()

        assert result.expired_ready_cleaned >= 1
        assert result.deleted_s3_objects >= 1
        assert result.storage_errors == 0
        assert result.db_errors == 0

        # Verify object is physically gone from MinIO
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as missing:
            s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        # Verify row is physically gone from PostgreSQL
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM shares WHERE share_token_hash = %s", (token_hash,))
        assert cur.fetchone()[0] == 0
        cur.close()

        # Verify API returns 404
        meta_resp = await client.get(f"/api/v1/shares/{share_token}")
        assert meta_resp.status_code == 404
        down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert down_resp.status_code == 404


@pytest.mark.asyncio
async def test_real_unexpired_file_preserved(session_factory, s3_client, db_conn, cleanup_harness):
    """Active unexpired files are never deleted by cleanup worker."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        payload = b"Preserve active unexpired file: " + secrets.token_bytes(32)
        files = {"file": ("keep_me.txt", payload, "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201

        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        cur = db_conn.cursor()
        cur.execute("SELECT object_key FROM shares WHERE share_token_hash = %s", (token_hash,))
        object_key = cur.fetchone()[0]
        cur.close()

        # Run cleanup cycle
        service = CleanupService(session_factory=session_factory)
        result = await service.run_cleanup_cycle()

        # The active file must not be counted in deletions
        # (Other background files could be 0, but this file must remain intact)
        assert result.storage_errors == 0

        # Object still exists in MinIO
        head_resp = s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        assert head_resp["ResponseMetadata"]["HTTPStatusCode"] == 200

        # Row still exists in Postgres in READY status
        cur = db_conn.cursor()
        cur.execute(
            "SELECT status FROM shares WHERE share_token_hash = %s",
            (token_hash,),
        )
        assert cur.fetchone()[0] == "ready"
        cur.close()

        # API still serves the file
        down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert down_resp.status_code == 200
        assert down_resp.content == payload


@pytest.mark.asyncio
async def test_real_owner_revocation_and_worker_cleanup(
    session_factory, s3_client, db_conn, cleanup_harness
):
    """Owner revokes access via DELETE endpoint; worker physically removes bytes and DB row."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        payload = b"Revoke owner file: " + secrets.token_bytes(32)
        files = {"file": ("revoked.txt", payload, "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201

        res_data = upload_resp.json()
        share_token = res_data["share_url"].split("/s/")[-1]
        manage_token = res_data["manage_token"]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        cur = db_conn.cursor()
        cur.execute("SELECT object_key FROM shares WHERE share_token_hash = %s", (token_hash,))
        object_key = cur.fetchone()[0]
        cur.close()

        # 1. Owner requests revocation via API
        del_resp = await client.delete(
            f"/api/v1/shares/{share_token}",
            headers={"X-Manage-Token": manage_token},
        )
        assert del_resp.status_code == 202
        assert del_resp.json()["detail"] == "Share deletion scheduled"

        # 2. Check DB status is immediately 'deleting'
        cur = db_conn.cursor()
        cur.execute("SELECT status FROM shares WHERE share_token_hash = %s", (token_hash,))
        assert cur.fetchone()[0] == "deleting"
        cur.close()

        # 3. Immediately blocked from download and metadata
        meta_resp = await client.get(f"/api/v1/shares/{share_token}")
        assert meta_resp.status_code == 404
        down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert down_resp.status_code == 404

        # 4. Worker executes physical cleanup
        service = CleanupService(session_factory=session_factory)
        result = await service.run_cleanup_cycle()

        assert result.owner_revoked_cleaned >= 1
        assert result.storage_errors == 0

        # 5. Object deleted from MinIO
        from botocore.exceptions import ClientError
        with pytest.raises(ClientError) as missing:
            s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404

        # 6. Row deleted from Postgres
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM shares WHERE share_token_hash = %s", (token_hash,))
        assert cur.fetchone()[0] == 0
        cur.close()


@pytest.mark.asyncio
async def test_real_stale_upload_recovery(session_factory, s3_client, db_conn, cleanup_harness):
    """Stale uncompleted upload records (past timeout) and partial S3 objects are recovered."""
    stale_key = f"stale-obj-{secrets.token_hex(16)}"
    cleanup_harness.append(stale_key)

    # Put a probe object in MinIO as if upload started
    s3_client.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=stale_key,
        Body=b"partial abandoned bytes",
    )

    # Insert a stale uploading record in Postgres created 2 hours ago
    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    manage_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    share_id = uuid.uuid4()

    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO shares (id, share_token_hash, manage_token_hash, object_key, "
        "original_filename, ttl_seconds, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'uploading', "
        "NOW() - INTERVAL '2 hours', NOW() - INTERVAL '2 hours')",
        (str(share_id), token_hash, manage_hash, stale_key, "abandoned.bin", 3600),
    )
    db_conn.commit()
    cur.close()

    # Run cleanup service with stale_upload_seconds=3600 (1 hour)
    service = CleanupService(session_factory=session_factory, stale_upload_seconds=3600)
    result = await service.run_cleanup_cycle()

    assert result.stale_uploading_cleaned >= 1
    assert result.storage_errors == 0

    # Object deleted from MinIO
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError) as missing:
        s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=stale_key)
    assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404

    # DB record deleted
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM shares WHERE id = %s", (str(share_id),))
    assert cur.fetchone()[0] == 0
    cur.close()


@pytest.mark.asyncio
async def test_real_active_upload_not_touched(
    session_factory, s3_client, db_conn, cleanup_harness
):
    """Recent uploading records (within timeout) are NOT treated as stale and are preserved."""
    active_key = f"active-upload-{secrets.token_hex(16)}"
    cleanup_harness.append(active_key)

    s3_client.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=active_key,
        Body=b"active in-flight bytes",
    )

    token_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    manage_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    share_id = uuid.uuid4()

    # Insert an uploading record created 30 seconds ago
    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO shares (id, share_token_hash, manage_token_hash, object_key, "
        "original_filename, ttl_seconds, status, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'uploading', "
        "NOW() - INTERVAL '30 seconds', NOW() - INTERVAL '30 seconds')",
        (str(share_id), token_hash, manage_hash, active_key, "in_flight.bin", 3600),
    )
    db_conn.commit()
    cur.close()

    service = CleanupService(session_factory=session_factory, stale_upload_seconds=3600)
    result = await service.run_cleanup_cycle()

    # Must NOT delete active upload
    assert result.stale_uploading_cleaned == 0

    # Object must still be in MinIO
    head_resp = s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=active_key)
    assert head_resp["ResponseMetadata"]["HTTPStatusCode"] == 200

    # DB record must still exist
    cur = db_conn.cursor()
    cur.execute("SELECT status FROM shares WHERE id = %s", (str(share_id),))
    assert cur.fetchone()[0] == "uploading"
    cur.close()


@pytest.mark.asyncio
async def test_real_repeated_cleanup_is_idempotent(session_factory):
    """Repeated execution on an already-clean environment produces 0 deletions and 0 errors."""
    service = CleanupService(session_factory=session_factory)
    result1 = await service.run_cleanup_cycle()
    result2 = await service.run_cleanup_cycle()

    assert result1.storage_errors == 0
    assert result2.total_cleaned == 0
    assert result2.storage_errors == 0
    assert result2.db_errors == 0


@pytest.mark.asyncio
async def test_real_concurrent_cleanup_workers(
    session_factory, s3_client, db_conn, cleanup_harness
):
    """Multiple concurrent workers process disjoint candidates via FOR UPDATE SKIP LOCKED."""
    # Create 6 expired shares directly in DB and MinIO
    created_keys = []
    for _ in range(6):
        key = f"concurrent-clean-{secrets.token_hex(16)}"
        created_keys.append(key)
        cleanup_harness.append(key)
        s3_client.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=b"payload")

        t_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        m_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO shares (id, share_token_hash, manage_token_hash, object_key, "
            "original_filename, size_bytes, ttl_seconds, status, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready', "
            "NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour')",
            (str(uuid.uuid4()), t_hash, m_hash, key, "concurrent.bin", 7, 3600),
        )
        db_conn.commit()
        cur.close()

    service1 = CleanupService(session_factory=session_factory)
    service2 = CleanupService(session_factory=session_factory)
    service3 = CleanupService(session_factory=session_factory)

    # Launch 3 workers concurrently
    results = await asyncio.gather(
        service1.run_cleanup_cycle(),
        service2.run_cleanup_cycle(),
        service3.run_cleanup_cycle(),
    )

    total_cleaned = sum(r.total_cleaned for r in results)
    total_storage_errors = sum(r.storage_errors for r in results)
    total_db_errors = sum(r.db_errors for r in results)

    assert total_cleaned >= 6
    assert total_storage_errors == 0
    assert total_db_errors == 0

    # Verify all 6 objects are deleted
    from botocore.exceptions import ClientError
    for key in created_keys:
        with pytest.raises(ClientError):
            s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)


@pytest.mark.asyncio
async def test_real_storage_failure_recovery_flow(
    session_factory, s3_client, db_conn, cleanup_harness
):
    """Storage failure keeps record in DELETING status; subsequent successful run removes it."""
    key = f"storage-fail-{secrets.token_hex(16)}"
    cleanup_harness.append(key)
    s3_client.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=b"failure probe")

    t_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    m_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    share_id = uuid.uuid4()

    cur = db_conn.cursor()
    cur.execute(
        "INSERT INTO shares (id, share_token_hash, manage_token_hash, object_key, "
        "original_filename, size_bytes, ttl_seconds, status, created_at, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready', "
        "NOW() - INTERVAL '2 hours', NOW() - INTERVAL '1 hour')",
        (str(share_id), t_hash, m_hash, key, "fail.bin", 13, 3600),
    )
    db_conn.commit()
    cur.close()

    # Mock storage to fail on delete_object
    mock_failing_storage = S3StorageAdapter()
    mock_failing_storage.delete_object = AsyncMock(return_value=False)

    service_fail = CleanupService(
        session_factory=session_factory,
        storage=mock_failing_storage,
    )
    res_fail = await service_fail.run_cleanup_cycle()

    assert res_fail.storage_errors >= 1
    assert res_fail.deleted_db_rows == 0

    # Verify DB record transitioned to DELETING but was NOT deleted
    cur = db_conn.cursor()
    cur.execute("SELECT status FROM shares WHERE id = %s", (str(share_id),))
    assert cur.fetchone()[0] == "deleting"
    cur.close()

    # Now run with real working storage: recovery succeeds!
    service_real = CleanupService(session_factory=session_factory)
    res_recover = await service_real.run_cleanup_cycle()

    assert res_recover.deleted_db_rows >= 1
    assert res_recover.storage_errors == 0

    # DB record is now deleted
    cur = db_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM shares WHERE id = %s", (str(share_id),))
    assert cur.fetchone()[0] == 0
    cur.close()


@pytest.mark.asyncio
async def test_real_orphan_s3_reconciliation(session_factory, s3_client, cleanup_harness):
    """Reconciliation discovers and deletes orphan S3 objects absent from database."""
    orphan_key = f"orphan-s3-{secrets.token_hex(16)}"
    cleanup_harness.append(orphan_key)

    s3_client.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=orphan_key,
        Body=b"orphan without DB entry",
    )

    service = CleanupService(session_factory=session_factory, orphan_grace_seconds=0)
    result = await service.run_cleanup_cycle(reconcile_orphans=True)

    assert result.orphan_s3_objects_cleaned >= 1

    # Verify orphan object deleted
    from botocore.exceptions import ClientError
    with pytest.raises(ClientError):
        s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=orphan_key)
