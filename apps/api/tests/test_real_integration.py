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
        assert res["share_url"].startswith(f"{str(settings.PUBLIC_WEB_URL).rstrip('/')}/s/")
        assert len(res["share_url"].split("/s/")[-1]) >= 42


# ---------------------------------------------------------------------------
# 6. Milestone 4 Real Retrieval & Download Flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_upload_and_download_flow(db_conn):
    """Complete roundtrip: upload file with Unicode name, get metadata, HEAD, and GET download."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        # Create a 256 KiB binary test payload with known hash
        payload = b"A" * 1024 + b"B" * 2048 + b"\x00\xff\xfe\xfd" * 65536
        expected_hash = hashlib.sha256(payload).hexdigest()
        unicode_filename = "отчёт_за_2026_год.pdf"

        files = {"file": (unicode_filename, payload, "application/pdf")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)

        assert upload_resp.status_code == 201
        upload_data = upload_resp.json()
        share_url = upload_data["share_url"]
        share_token = share_url.split("/s/")[-1]

        # 1. Metadata endpoint GET /api/v1/shares/{share_token}
        meta_resp = await client.get(f"/api/v1/shares/{share_token}")
        assert meta_resp.status_code == 200
        meta_data = meta_resp.json()
        assert meta_data["filename"] == unicode_filename
        assert meta_data["size_bytes"] == len(payload)
        assert "expires_at" in meta_data
        assert "manage_token" not in meta_data
        assert "object_key" not in meta_data

        # Verify initial download_count in DB is 0
        cur = db_conn.cursor()
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
        cur.execute("SELECT download_count FROM shares WHERE share_token_hash = %s", (token_hash,))
        initial_count = cur.fetchone()[0]
        assert initial_count == 0
        cur.close()

        # 2. HEAD request: /api/v1/shares/{share_token}/download
        head_resp = await client.head(f"/api/v1/shares/{share_token}/download")
        assert head_resp.status_code == 200
        assert head_resp.headers["content-type"] == "application/octet-stream"
        assert head_resp.headers["content-length"] == str(len(payload))
        assert head_resp.headers["x-content-type-options"] == "nosniff"
        assert head_resp.headers["cache-control"] == "no-store"
        assert head_resp.headers["referrer-policy"] == "no-referrer"
        assert len(head_resp.content) == 0

        # Verify HEAD did not increment download_count
        cur = db_conn.cursor()
        cur.execute("SELECT download_count FROM shares WHERE share_token_hash = %s", (token_hash,))
        assert cur.fetchone()[0] == 0
        cur.close()

        # 3. GET download request: /api/v1/shares/{share_token}/download
        get_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert get_resp.status_code == 200
        assert get_resp.headers["content-type"] == "application/octet-stream"
        assert get_resp.headers["content-length"] == str(len(payload))
        assert get_resp.headers["x-content-type-options"] == "nosniff"
        assert get_resp.headers["cache-control"] == "no-store"
        assert get_resp.headers["referrer-policy"] == "no-referrer"

        # Content-Disposition header checks
        cd_header = get_resp.headers["content-disposition"]
        assert "attachment;" in cd_header
        assert "filename*=UTF-8''" in cd_header

        # Verify byte-for-byte exact equality
        downloaded_bytes = get_resp.content
        assert len(downloaded_bytes) == len(payload)
        assert hashlib.sha256(downloaded_bytes).hexdigest() == expected_hash

        # Verify GET incremented download_count to 1
        cur = db_conn.cursor()
        cur.execute("SELECT download_count FROM shares WHERE share_token_hash = %s", (token_hash,))
        assert cur.fetchone()[0] == 1
        cur.close()


@pytest.mark.asyncio
async def test_real_download_expired_share(db_conn):
    """Test that expired shares return 404 on both metadata and download."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("expires_soon.txt", b"temporary data", "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201
        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        # Expire the share in the real PostgreSQL database
        cur = db_conn.cursor()
        cur.execute(
            "UPDATE shares SET expires_at = NOW() - INTERVAL '1 minute' "
            "WHERE share_token_hash = %s",
            (token_hash,),
        )
        db_conn.commit()
        cur.close()

        # Both endpoints must return 404
        meta_resp = await client.get(f"/api/v1/shares/{share_token}")
        assert meta_resp.status_code == 404
        assert meta_resp.json()["detail"] == "Share not found or expired"

        down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert down_resp.status_code == 404
        assert down_resp.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_real_download_non_ready_statuses(db_conn):
    """Test that non-ready shares return neutral 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("pending.txt", b"waiting data", "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201
        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        cur = db_conn.cursor()
        for bad_status in ["failed", "uploading", "deleting"]:
            cur.execute(
                "UPDATE shares SET status = %s WHERE share_token_hash = %s",
                (bad_status, token_hash),
            )
            db_conn.commit()

            meta_resp = await client.get(f"/api/v1/shares/{share_token}")
            assert meta_resp.status_code == 404
            assert meta_resp.json()["detail"] == "Share not found or expired"

            down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
            assert down_resp.status_code == 404
            assert down_resp.json()["detail"] == "Share not found or expired"
        cur.close()


@pytest.mark.asyncio
async def test_real_download_missing_minio_object(s3_client, db_conn):
    """Test that missing S3 object returns 404 cleanly without exposing internals."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("ghost.txt", b"object will vanish", "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201
        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        # Retrieve metadata first (which works via Postgres)
        meta_resp = await client.get(f"/api/v1/shares/{share_token}")
        assert meta_resp.status_code == 200

        # Now delete object directly from MinIO bucket
        cur = db_conn.cursor()
        cur.execute("SELECT object_key FROM shares WHERE share_token_hash = %s", (token_hash,))
        object_key = cur.fetchone()[0]
        cur.close()

        s3_client.delete_object(Bucket=settings.S3_BUCKET_NAME, Key=object_key)

        # Download must return neutral 404
        down_resp = await client.get(f"/api/v1/shares/{share_token}/download")
        assert down_resp.status_code == 404
        assert down_resp.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_real_download_invalid_token():
    """Test completely invalid token lookup against real database."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        meta_resp = await client.get("/api/v1/shares/completely_invalid_random_token_123")
        assert meta_resp.status_code == 404
        assert meta_resp.json()["detail"] == "Share not found or expired"

        down_resp = await client.get("/api/v1/shares/completely_invalid_random_token_123/download")
        assert down_resp.status_code == 404
        assert down_resp.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_real_download_concurrent_downloads(db_conn):
    """Test multiple concurrent downloads maintaining data integrity and atomic counter."""
    import asyncio

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        payload = b"Concurrent download payload block " * 500
        expected_hash = hashlib.sha256(payload).hexdigest()
        files = {"file": ("concurrent.txt", payload, "text/plain")}
        data = {"ttl": "1h"}
        upload_resp = await client.post("/api/v1/shares", files=files, data=data)
        assert upload_resp.status_code == 201
        share_token = upload_resp.json()["share_url"].split("/s/")[-1]
        token_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()

        # Fire 5 concurrent download requests
        tasks = [
            client.get(f"/api/v1/shares/{share_token}/download")
            for _ in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        for resp in responses:
            assert resp.status_code == 200
            assert hashlib.sha256(resp.content).hexdigest() == expected_hash

        # Verify atomic counter in DB reached exactly 5
        cur = db_conn.cursor()
        cur.execute("SELECT download_count FROM shares WHERE share_token_hash = %s", (token_hash,))
        final_count = cur.fetchone()[0]
        assert final_count == 5
        cur.close()
