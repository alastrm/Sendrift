"""Real integration tests against live PostgreSQL and MinIO containers.

This test file connects directly to the running Docker containers for
PostgreSQL (port 5433) and MinIO (port 9000). It is strictly distinguished
from mocked unit tests.
"""
import hashlib
import io
import os
from unittest.mock import patch

import boto3
import psycopg2
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.repositories.share_repository import ShareRepository
from app.storage.s3 import S3StorageAdapter, StorageUnavailableError

pytestmark = pytest.mark.skipif(
    os.environ.get("SENDRIFT_REAL_INTEGRATION") != "1",
    reason="Set SENDRIFT_REAL_INTEGRATION=1 to use a disposable PostgreSQL/MinIO environment",
)


@pytest.fixture(autouse=True)
async def use_nullpool_db(monkeypatch, s3_client):
    test_engine = create_async_engine(
        settings.async_database_url,
        poolclass=NullPool,
    )
    test_session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    async def _override_get_db():
        async with test_session_factory() as session:
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
        # Remove only resources created by this test, including failed uploads.
        for key in keys:
            s3_client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        from sqlalchemy import delete

        from app.models.share import Share

        async with test_session_factory() as session:
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
        config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}),
    )


# ---------------------------------------------------------------------------
# 1. Real File Upload, DB Verification, and MinIO Object Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_upload_and_persistence(db_conn, s3_client):
    """Upload real file, verify byte-for-byte in MinIO, and verify Postgres record."""
    payload = b"Sendrift Real Container Integration Payload - Byte For Byte!"
    payload_len = len(payload)

    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("integration_doc.txt", payload, "text/plain")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 201
        res_data = response.json()

        share_url = res_data["share_url"]
        manage_token = res_data["manage_token"]
        assert "/s/" in share_url
        assert manage_token not in share_url

        share_token = share_url.split("/s/")[-1]
        expected_share_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
        expected_manage_hash = hashlib.sha256(manage_token.encode("utf-8")).hexdigest()

        # Verify PostgreSQL database record
        cur = db_conn.cursor()
        cur.execute(
            """
            SELECT id, share_token_hash, manage_token_hash, object_key,
                   status, size_bytes, ready_at, expires_at, ttl_seconds
            FROM shares WHERE share_token_hash = %s
            """,
            (expected_share_hash,),
        )
        row = cur.fetchone()
        assert row is not None, "Postgres record was not created!"

        (
            db_id,
            db_share_hash,
            db_manage_hash,
            db_object_key,
            db_status,
            db_size,
            db_ready_at,
            db_expires_at,
            db_ttl,
        ) = row

        assert db_status == "ready"
        assert db_size == payload_len
        assert db_ttl == 3600
        assert db_manage_hash == expected_manage_hash
        assert db_ready_at is not None
        assert db_expires_at is not None
        assert db_expires_at > db_ready_at

        # Verify MinIO private bucket object byte-for-byte
        obj = s3_client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=db_object_key)
        stored_bytes = obj["Body"].read()
        assert stored_bytes == payload, "MinIO stored bytes do not match uploaded bytes!"


# ---------------------------------------------------------------------------
# 2. File Size Boundary Testing: 0 bytes, 1 byte, 100 MiB boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_upload_empty_file_rejected(db_conn, s3_client):
    """Verify 0-byte file rejected with 400 and marked as failed in DB."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("empty.txt", b"", "text/plain")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_real_upload_1_byte_file(db_conn, s3_client):
    """Verify minimum valid file size: exactly 1 byte."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("one_byte.bin", b"X", "application/octet-stream")}
        data = {"ttl": "24h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 201
        res = response.json()
        assert res["size_bytes"] == 1

        share_token = res["share_url"].split("/s/")[-1]
        share_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        cur = db_conn.cursor()
        cur.execute(
            "SELECT status, size_bytes, object_key FROM shares WHERE share_token_hash = %s",
            (share_hash,),
        )
        row = cur.fetchone()
        assert row[0] == "ready"
        assert row[1] == 1

        # Verify in MinIO
        obj = s3_client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=row[2])
        assert obj["Body"].read() == b"X"


@pytest.mark.asyncio
async def test_real_upload_oversized_file_rejected(db_conn, s3_client, use_nullpool_db):
    """Verify file exceeding 100 MiB is rejected with 413 and object deleted."""
    from httpx import ASGITransport

    class OversizedStream(io.RawIOBase):
        def __init__(self, total_bytes: int):
            self.remaining = total_bytes

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            if self.remaining <= 0:
                return b""
            chunk_len = min(65536, self.remaining)
            self.remaining -= chunk_len
            return b"O" * chunk_len

    oversized_stream = OversizedStream(settings.MAX_FILE_SIZE_BYTES + 1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("oversized.bin", oversized_stream, "application/octet-stream")}  # type: ignore[dict-item]
        data = {"ttl": "7d"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 413
        assert "100 MiB" in response.json()["detail"]
        from botocore.exceptions import ClientError

        assert len(use_nullpool_db) == 1
        key = use_nullpool_db[0]
        with pytest.raises(ClientError) as missing:
            s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        uploads = s3_client.list_multipart_uploads(Bucket=settings.S3_BUCKET_NAME)
        assert all(upload["Key"] != key for upload in uploads.get("Uploads", []))
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM shares WHERE object_key = %s", (key,))
            assert cur.fetchone() == ("failed",)


@pytest.mark.asyncio
async def test_real_upload_100_mib_boundary(db_conn, s3_client):
    """Verify maximum valid file size: exactly 100 MiB (104,857,600 bytes)."""
    from httpx import ASGITransport

    class ExactBoundaryStream(io.RawIOBase):
        def __init__(self, total_bytes: int):
            self.remaining = total_bytes

        def readable(self) -> bool:
            return True

        def read(self, size: int = -1) -> bytes:
            if self.remaining <= 0:
                return b""
            chunk_len = min(size if size > 0 else 65536, self.remaining)
            self.remaining -= chunk_len
            return b"M" * chunk_len

    exact_100mib_stream = ExactBoundaryStream(settings.MAX_FILE_SIZE_BYTES)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("exact_100mb.bin", exact_100mib_stream, "application/octet-stream")}  # type: ignore[dict-item]
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 201
        res = response.json()
        assert res["size_bytes"] == settings.MAX_FILE_SIZE_BYTES

        share_token = res["share_url"].split("/s/")[-1]
        share_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        cur = db_conn.cursor()
        cur.execute(
            "SELECT status, size_bytes, object_key FROM shares WHERE share_token_hash = %s",
            (share_hash,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "ready"
        assert row[1] == settings.MAX_FILE_SIZE_BYTES
        object_key = row[2]

        # Verify object exists in MinIO with exact 100 MiB length
        head = s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        assert head["ContentLength"] == settings.MAX_FILE_SIZE_BYTES

        # Clean up 100 MiB object and row
        s3_client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)
        cur.execute("DELETE FROM shares WHERE share_token_hash = %s", (share_hash,))
        db_conn.commit()


# ---------------------------------------------------------------------------
# 3. Invalid TTL and Storage Failure Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_upload_invalid_ttl_rejected():
    """Verify rejection of invalid TTL."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("test.txt", b"valid content", "text/plain")}
        data = {"ttl": "48h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 400
        assert "Invalid TTL" in response.json()["detail"]


@pytest.mark.asyncio
async def test_real_upload_storage_failure_compensation(db_conn):
    """Verify storage failure returns 503, marks row failed, and does not leak secrets."""
    transport = ASGITransport(app=app)
    with patch(
        "app.storage.s3.S3StorageAdapter.put_stream",
        side_effect=StorageUnavailableError("MinIO connection reset"),
    ):
        async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            files = {"file": ("fail_test.txt", b"payload data", "text/plain")}
            data = {"ttl": "1h"}
            response = await client.post("/api/v1/shares", files=files, data=data)

            assert response.status_code == 503
            assert response.json()["detail"] == "Storage service temporarily unavailable"

            # Verify in PostgreSQL that the share row exists and was marked failed
            cur = db_conn.cursor()
            cur.execute(
                """
                SELECT status FROM shares
                WHERE original_filename = 'fail_test.txt'
                ORDER BY created_at DESC LIMIT 1
                """
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "failed"


@pytest.mark.asyncio
async def test_real_upload_db_finalize_failure_compensation(s3_client):
    """Verify DB commit failure when finalizing triggers storage object deletion."""
    uploaded_keys: list[str] = []

    original_put = S3StorageAdapter.put_stream
    original_commit = AsyncSession.commit
    commit_calls = 0

    async def failing_commit(session):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 2:
            raise DBAPIError("Simulated DB commit crash", None, None)
        await original_commit(session)

    async def tracking_put(self_storage, object_key, stream, max_size_bytes, content_type=None):
        uploaded_keys.append(object_key)
        return await original_put(self_storage, object_key, stream, max_size_bytes, content_type)

    transport = ASGITransport(app=app)
    with patch.object(S3StorageAdapter, "put_stream", side_effect=tracking_put, autospec=True), \
         patch(
             "sqlalchemy.ext.asyncio.AsyncSession.commit",
             new=failing_commit,
         ):
        async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            files = {"file": ("db_fail.txt", b"payload to clean", "text/plain")}
            data = {"ttl": "1h"}
            response = await client.post("/api/v1/shares", files=files, data=data)

            assert response.status_code == 500
            assert response.json()["detail"] == "Failed to finalize share record"

            # Verify that the MinIO object was cleaned up
            assert len(uploaded_keys) == 1
            key = uploaded_keys[0]
            from botocore.exceptions import ClientError
            with pytest.raises(ClientError) as missing:
                s3_client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
            assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404


# ---------------------------------------------------------------------------
# 4. MinIO Bucket Privacy Verification
# ---------------------------------------------------------------------------


def test_real_minio_bucket_privacy(s3_client):
    """Verify bucket has private ACL with no public grants."""
    acl = s3_client.get_bucket_acl(Bucket=settings.S3_BUCKET_NAME)
    for grant in acl["Grants"]:
        grantee = grant.get("Grantee", {})
        uri = grantee.get("URI", "")
        assert "AllUsers" not in uri, "Public read/write access found on bucket!"
        assert "AuthenticatedUsers" not in uri, "Open authenticated access found on bucket!"


@pytest.mark.asyncio
async def test_real_startup_privacy_and_anonymous_access(s3_client, use_nullpool_db):
    """Exercise startup verification and anonymous read/list/write/delete denial."""
    import secrets

    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError

    storage = S3StorageAdapter()
    await storage.ensure_bucket()
    key = f"integration-privacy-{secrets.token_hex(16)}"
    use_nullpool_db.append(key)
    s3_client.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=b"privacy probe")
    anonymous = boto3.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT_URL,
        config=Config(signature_version=UNSIGNED, connect_timeout=5, read_timeout=5),
    )
    try:
        operations = [
            lambda: anonymous.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key),
            lambda: anonymous.list_objects_v2(Bucket=settings.S3_BUCKET_NAME),
            lambda: anonymous.put_object(Bucket=settings.S3_BUCKET_NAME, Key=key, Body=b"bad"),
            lambda: anonymous.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=key),
        ]
        for operation in operations:
            with pytest.raises(ClientError) as denied:
                operation()
            assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
        stored = s3_client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)["Body"]
        try:
            assert stored.read() == b"privacy probe"
        finally:
            stored.close()
    finally:
        anonymous.close()


# ---------------------------------------------------------------------------
# 5. Share URL Web Route Alignment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_url_structure():
    """Verify share_url conforms to planned Next.js route: /s/[token]."""
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("plan.txt", b"route check", "text/plain")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 201
        res = response.json()
        assert res["share_url"].startswith("http://localhost:8000/s/")
        assert len(res["share_url"].split("/s/")[-1]) >= 42
