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
from app.services.share_service import ShareService
from app.storage.s3 import S3StorageAdapter


@pytest.fixture
def sample_share():
    raw_share_token = "valid-share-token-12345"
    raw_manage_token = "valid-manage-token-67890"
    share_hash = hashlib.sha256(raw_share_token.encode("utf-8")).hexdigest()
    manage_hash = hashlib.sha256(raw_manage_token.encode("utf-8")).hexdigest()

    share = Share(
        id=uuid.uuid4(),
        share_token_hash=share_hash,
        manage_token_hash=manage_hash,
        object_key="obj-key-123",
        original_filename="confidential.pdf",
        content_type="application/pdf",
        ttl_seconds=3600,
        status=ShareStatus.READY,
        size_bytes=1024,
        download_count=1,
        ready_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=50),
    )
    return share, raw_share_token, raw_manage_token


@pytest.mark.asyncio
async def test_owner_delete_success(sample_share):
    """Owner successfully revokes share using valid X-Manage-Token header."""
    share, raw_share_token, raw_manage_token = sample_share

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=share)
    mock_share_repo.mark_deleting = AsyncMock(return_value=share)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/shares/{raw_share_token}",
            headers={"X-Manage-Token": raw_manage_token},
        )
        assert response.status_code == 202
        assert response.json()["detail"] == "Share deletion scheduled"
        mock_share_repo.mark_deleting.assert_awaited_once_with(share.id)


@pytest.mark.asyncio
async def test_owner_delete_idempotent_already_deleting(sample_share):
    """Repeated DELETE on already-deleting share returns 202 Accepted safely."""
    share, raw_share_token, raw_manage_token = sample_share
    share.status = ShareStatus.DELETING

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=share)
    mock_share_repo.mark_deleting = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/shares/{raw_share_token}",
            headers={"X-Manage-Token": raw_manage_token},
        )
        assert response.status_code == 202
        assert response.json()["detail"] == "Share deletion scheduled"
        # Since already deleting, no need to update status again
        mock_share_repo.mark_deleting.assert_not_called()


@pytest.mark.asyncio
async def test_owner_delete_missing_manage_token(sample_share):
    """Missing X-Manage-Token header returns neutral 404 without leaking info."""
    _, raw_share_token, _ = sample_share

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(f"/api/v1/shares/{raw_share_token}")
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"


@pytest.mark.asyncio
async def test_owner_delete_invalid_manage_token(sample_share):
    """Incorrect manage token returns 404 with neutral message."""
    share, raw_share_token, _ = sample_share

    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=share)
    mock_share_repo.mark_deleting = AsyncMock()

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/shares/{raw_share_token}",
            headers={"X-Manage-Token": "completely-wrong-token-abc"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"
        mock_share_repo.mark_deleting.assert_not_called()


@pytest.mark.asyncio
async def test_owner_delete_nonexistent_share():
    """Non-existent share token returns 404."""
    mock_storage = MagicMock(spec=S3StorageAdapter)
    mock_share_repo = MagicMock(spec=ShareRepository)
    mock_share_repo.get_by_share_token_hash = AsyncMock(return_value=None)

    service = ShareService(share_repo=mock_share_repo, storage=mock_storage)
    app.dependency_overrides[get_share_service] = lambda: service

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/api/v1/shares/nonexistent-token",
            headers={"X-Manage-Token": "some-manage-token"},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Share not found or expired"
