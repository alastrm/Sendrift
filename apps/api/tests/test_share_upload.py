import hashlib
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.shares import get_share_service
from app.main import app
from app.models.share import Share, ShareStatus
from app.repositories.share_repository import ShareRepository
from app.schemas.share import TTL_SECONDS_MAP
from app.services.share_service import (
    ShareService,
    generate_object_key,
    generate_tokens,
    sanitize_filename,
)
from app.storage.s3 import (
    BoundedStreamReader,
    EmptyFileError,
    FileSizeLimitExceededError,
    S3StorageAdapter,
    StorageUnavailableError,
)

# ---------------------------------------------------------------------------
# Unit Tests: Cryptographic tokens, hashing, and filename sanitization
# ---------------------------------------------------------------------------


def test_token_generation_and_hashing_secrecy():
    """Verify high entropy tokens and SHA-256 storage hashes."""
    share_token, manage_token, share_hash, manage_hash = generate_tokens()

    # Tokens must be distinct
    assert share_token != manage_token
    assert share_hash != manage_hash

    # Length of urlsafe base64 tokens for 32 bytes must be >= 42 chars (>=256 bits entropy)
    assert len(share_token) >= 42
    assert len(manage_token) >= 42

    # Hashes must be exact 64-char hex strings of SHA-256
    expected_share_hash = hashlib.sha256(share_token.encode("utf-8")).hexdigest()
    expected_manage_hash = hashlib.sha256(manage_token.encode("utf-8")).hexdigest()

    assert share_hash == expected_share_hash
    assert manage_hash == expected_manage_hash

    # Manage token must never match or be contained in share hash
    assert manage_token not in share_hash
    assert share_token not in manage_hash


def test_generate_object_key_unpredictability():
    """Verify object keys are random and independent of file metadata."""
    key1 = generate_object_key()
    key2 = generate_object_key()
    assert key1 != key2
    assert len(key1) == 32  # 16 bytes hex


def test_sanitize_filename_edge_cases():
    """Verify paths and malicious patterns are stripped safely."""
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
    assert sanitize_filename("safe_report.pdf") == "safe_report.pdf"
    assert sanitize_filename("file\x00with_null.txt") == "filewith_null.txt"
    assert sanitize_filename("") == "unnamed_file"
    assert sanitize_filename(None) == "unnamed_file"


def test_expiration_calculation():
    """Verify TTL mappings conform to 1h, 24h, 7d specification."""
    assert TTL_SECONDS_MAP["1h"] == 3600
    assert TTL_SECONDS_MAP["24h"] == 86400
    assert TTL_SECONDS_MAP["7d"] == 604800


# ---------------------------------------------------------------------------
# Storage Stream Tests: BoundedStreamReader
# ---------------------------------------------------------------------------


def test_bounded_stream_reader_normal():
    data = b"Hello, Sendrift!"
    stream = io.BytesIO(data)
    reader = BoundedStreamReader(stream, max_bytes=100)
    read_data = reader.read()
    assert read_data == data
    assert reader.bytes_read == len(data)


def test_bounded_stream_reader_exceeds_limit():
    data = b"A" * 105
    stream = io.BytesIO(data)
    reader = BoundedStreamReader(stream, max_bytes=100)
    with pytest.raises(FileSizeLimitExceededError):
        reader.read()


# ---------------------------------------------------------------------------
# API Upload Flow Tests (Mocked Integration)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_storage():
    storage = MagicMock(spec=S3StorageAdapter)
    storage.put_stream = AsyncMock(return_value=1024)
    storage.delete_object = AsyncMock(return_value=True)
    return storage


@pytest.fixture
def sample_share():
    return Share(
        id=uuid.uuid4(),
        share_token_hash="a" * 64,
        manage_token_hash="b" * 64,
        object_key="randkey123",
        original_filename="sample.txt",
        ttl_seconds=3600,
        status=ShareStatus.UPLOADING,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_api_upload_success(mock_storage, sample_share):
    """Test standard 201 Created response and correct metadata fields."""
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.create_uploading = AsyncMock(return_value=sample_share)

    ready_share = Share(
        id=sample_share.id,
        share_token_hash=sample_share.share_token_hash,
        manage_token_hash=sample_share.manage_token_hash,
        object_key=sample_share.object_key,
        original_filename=sample_share.original_filename,
        ttl_seconds=sample_share.ttl_seconds,
        status=ShareStatus.READY,
        size_bytes=1024,
        ready_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    mock_share_repo.mark_ready = AsyncMock(return_value=ready_share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
        files = {"file": ("test.txt", b"A" * 1024, "text/plain")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 201
        body = response.json()

        assert "share_url" in body
        assert "/s/" in body["share_url"]
        assert "manage_token" in body
        assert body["filename"] == "test.txt"
        assert body["size_bytes"] == 1024
        assert "expires_at" in body

        # manage_token must NOT be in the public share_url
        assert body["manage_token"] not in body["share_url"]


@pytest.mark.asyncio
async def test_api_upload_invalid_ttl():
    """Test rejection with 400 for unsupported TTL values."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("test.txt", b"content", "text/plain")}
        data = {"ttl": "2h"}  # Not allowed (only 1h, 24h, 7d)
        response = await client.post("/api/v1/shares", files=files, data=data)
        assert response.status_code == 400
        assert "Invalid TTL" in response.json()["detail"]


@pytest.mark.asyncio
async def test_api_upload_empty_file(sample_share):
    """Test rejection with 400 when uploaded file is 0 bytes."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.put_stream = AsyncMock(side_effect=EmptyFileError("File is empty (0 bytes)"))
    mock_storage.delete_object = AsyncMock()

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.create_uploading = AsyncMock(return_value=sample_share)
    mock_share_repo.mark_failed = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("empty.txt", b"", "text/plain")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()
        mock_share_repo.mark_failed.assert_awaited_once_with(sample_share.id)


@pytest.mark.asyncio
async def test_api_upload_oversized_file(sample_share):
    """Test rejection with 413 when file exceeds 100 MiB."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.put_stream = AsyncMock(
        side_effect=FileSizeLimitExceededError("File exceeds limit")
    )
    mock_storage.delete_object = AsyncMock()

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.create_uploading = AsyncMock(return_value=sample_share)
    mock_share_repo.mark_failed = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("big.bin", b"X" * 10, "application/octet-stream")}
        data = {"ttl": "24h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 413
        assert "100 MiB" in response.json()["detail"]
        mock_share_repo.mark_failed.assert_awaited_once_with(sample_share.id)


@pytest.mark.asyncio
async def test_api_upload_storage_unavailable(sample_share):
    """Test 503 response when storage is unreachable without leaking S3 internals."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_storage.put_stream = AsyncMock(
        side_effect=StorageUnavailableError("MinIO connection failed")
    )
    mock_storage.delete_object = AsyncMock()

    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.create_uploading = AsyncMock(return_value=sample_share)
    mock_share_repo.mark_failed = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("file.pdf", b"data", "application/pdf")}
        data = {"ttl": "7d"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 503
        assert response.json()["detail"] == "Storage service temporarily unavailable"
        mock_share_repo.mark_failed.assert_awaited_once_with(sample_share.id)


@pytest.mark.asyncio
async def test_api_upload_database_commit_failure(sample_share, mock_storage):
    """Test 500 and object compensation delete when DB commit fails on ready transition."""
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.create_uploading = AsyncMock(return_value=sample_share)
    mock_share_repo.mark_ready = AsyncMock(side_effect=Exception("DB connection broken"))

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        files = {"file": ("file.pdf", b"data", "application/pdf")}
        data = {"ttl": "1h"}
        response = await client.post("/api/v1/shares", files=files, data=data)

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to finalize share record"
        # Verify compensation: storage object must be deleted
        mock_storage.delete_object.assert_awaited_once()
        called_key = mock_storage.delete_object.call_args[0][0]
        assert len(called_key) == 32
