import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.shares import get_share_service
from app.main import app
from app.models.share import Share, ShareStatus
from app.repositories.share_repository import ShareRepository
from app.services.share_service import (
    ShareService,
    format_content_disposition,
    hash_token,
)
from app.storage.s3 import (
    ObjectNotFoundError,
    S3ObjectStream,
    S3StorageAdapter,
    StorageUnavailableError,
)

# ---------------------------------------------------------------------------
# Unit Tests: format_content_disposition and hash_token
# ---------------------------------------------------------------------------


def test_hash_token_deterministic_sha256():
    token = "secret-token-xyz-123"
    expected = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert hash_token(token) == expected
    assert len(hash_token(token)) == 64


def test_format_content_disposition_ascii():
    header = format_content_disposition("report_final.pdf")
    assert 'filename="report_final.pdf"' in header
    assert "filename*=UTF-8''report_final.pdf" in header
    assert header.startswith("attachment; ")


def test_format_content_disposition_unicode_cyrillic():
    header = format_content_disposition("отчёт_2026.pdf")
    # ASCII fallback escapes non-ascii
    assert "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82_2026.pdf" in header
    assert header.startswith("attachment; ")


def test_format_content_disposition_malicious_crlf_injection():
    """Ensure HTTP Response Splitting via CRLF injection is completely prevented."""
    malicious = "legit.pdf\r\nSet-Cookie: evil=session\r\n\r\n<script>"
    header = format_content_disposition(malicious)
    assert "\r" not in header
    assert "\n" not in header
    assert "Set-Cookie" not in header or "%0D%0A" not in header
    assert "\r\n" not in header


def test_format_content_disposition_quotes_and_slashes():
    raw = 'file"name\\test.txt'
    header = format_content_disposition(raw)
    assert "\n" not in header
    assert "\r" not in header
    assert header.startswith("attachment; ")


def test_format_content_disposition_empty_fallback():
    assert 'filename="download"' in format_content_disposition("")
    assert 'filename="download"' in format_content_disposition("   ")
    assert 'filename="download"' in format_content_disposition("\r\n\x00")


# ---------------------------------------------------------------------------
# Unit Tests: S3ObjectStream Bounded Streaming and Resource Cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_object_stream_normal_iteration():
    mock_body = MagicMock()
    mock_body.read.side_effect = [b"chunk1", b"chunk2", b""]
    mock_body.close = MagicMock()

    stream = S3ObjectStream(
        body=mock_body,
        content_length=12,
        content_type="application/octet-stream",
    )

    chunks = []
    async for chunk in stream.iter_bytes(chunk_size=6):
        chunks.append(chunk)

    assert chunks == [b"chunk1", b"chunk2"]
    mock_body.close.assert_called_once()


@pytest.mark.asyncio
async def test_s3_object_stream_cleanup_on_interruption():
    mock_body = MagicMock()
    mock_body.read.side_effect = [b"chunk1", b"chunk2"]
    mock_body.close = MagicMock()

    stream = S3ObjectStream(
        body=mock_body,
        content_length=12,
        content_type="application/octet-stream",
    )

    gen = stream.iter_bytes(chunk_size=6)
    with pytest.raises(RuntimeError, match="Client disconnected"):
        try:
            async for chunk in gen:
                if chunk == b"chunk1":
                    raise RuntimeError("Client disconnected")
        finally:
            await gen.aclose()

    mock_body.close.assert_called_once()


# ---------------------------------------------------------------------------
# Unit Tests: Share Metadata API (GET /api/v1/shares/{share_token})
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_ready_share():
    return Share(
        id=uuid.uuid4(),
        share_token_hash=hashlib.sha256(b"token-123").hexdigest(),
        manage_token_hash=hashlib.sha256(b"manage-123").hexdigest(),
        object_key="key-abc-123",
        original_filename="sample_document.pdf",
        content_type="application/pdf",
        ttl_seconds=3600,
        status=ShareStatus.READY,
        size_bytes=2048,
        download_count=5,
        ready_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=50),
    )


@pytest.mark.asyncio
async def test_get_metadata_success(sample_ready_share):
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123")
        assert response.status_code == 200
        body = response.json()
        assert body["filename"] == "sample_document.pdf"
        assert body["size_bytes"] == 2048
        assert "expires_at" in body
        assert "share_token_hash" not in body
        assert "manage_token_hash" not in body
        assert "object_key" not in body


@pytest.mark.asyncio
async def test_get_metadata_not_found():
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=None)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/nonexistent-token")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_val",
    [ShareStatus.UPLOADING, ShareStatus.FAILED, ShareStatus.DELETING],
)
async def test_get_metadata_non_ready_statuses(sample_ready_share, status_val):
    sample_ready_share.status = status_val
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_get_metadata_expired_share(sample_ready_share):
    sample_ready_share.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


# ---------------------------------------------------------------------------
# Unit Tests: Share Download API (GET and HEAD /api/v1/shares/{share_token}/download)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_share_success(sample_ready_share):
    file_bytes = b"Secret payload content for testing download stream!"
    mock_body = MagicMock()
    mock_body.read.side_effect = [file_bytes, b""]
    mock_body.close = MagicMock()

    mock_stream = S3ObjectStream(
        body=mock_body,
        content_length=len(file_bytes),
        content_type="application/octet-stream",
    )

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.get_stream = AsyncMock(return_value=mock_stream)

    sample_ready_share.size_bytes = len(file_bytes)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)
    mock_share_repo.increment_download_count = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123/download")
        assert response.status_code == 200
        assert response.content == file_bytes

        # Verify strict security headers
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-length"] == str(len(file_bytes))
        cd_hdr = response.headers["content-disposition"]
        assert 'attachment; filename="sample_document.pdf"' in cd_hdr

        # Verify download count incremented
        mock_share_repo.increment_download_count.assert_awaited_once_with(sample_ready_share.id)
        mock_body.close.assert_called_once()


@pytest.mark.asyncio
async def test_download_share_head_request(sample_ready_share):
    mock_body = MagicMock()
    mock_body.close = MagicMock()

    mock_stream = S3ObjectStream(
        body=mock_body,
        content_length=sample_ready_share.size_bytes,
        content_type="application/octet-stream",
    )

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.get_stream = AsyncMock(return_value=mock_stream)

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)
    mock_share_repo.increment_download_count = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.head("/api/v1/shares/token-123/download")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-length"] == str(sample_ready_share.size_bytes)
        assert "content-disposition" in response.headers

        # HEAD must NOT increment download count
        mock_share_repo.increment_download_count.assert_not_called()
        # Underlying body stream must be closed immediately
        mock_body.close.assert_called_once()


@pytest.mark.asyncio
async def test_download_share_not_found():
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=None)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/nonexistent/download")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_download_share_expired(sample_ready_share):
    sample_ready_share.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123/download")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_download_share_missing_s3_object(sample_ready_share):
    """When DB record exists but S3 object is missing, return 404 cleanly."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.get_stream = AsyncMock(side_effect=ObjectNotFoundError("Missing object"))

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123/download")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_download_share_storage_unavailable(sample_ready_share):
    """When MinIO has connectivity failure, return 503 Service Unavailable."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.get_stream = AsyncMock(
        side_effect=StorageUnavailableError("Cannot connect to storage")
    )

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=sample_ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/shares/token-123/download")
        assert response.status_code == 503
        assert response.json()["detail"] == "Storage service temporarily unavailable"
