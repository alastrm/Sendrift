from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_health_live_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["app"] == "Sendrift API"
        assert "timestamp" in data


@pytest.mark.asyncio
async def test_api_v1_health_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_ready_when_healthy():
    transport = ASGITransport(app=app)
    with (
        patch("app.services.health_service.check_db_health", new_callable=AsyncMock) as mock_db,
        patch("app.storage.s3.S3StorageAdapter.check_health", new_callable=AsyncMock) as mock_s3,
    ):
        mock_db.return_value = True
        mock_s3.return_value = True

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["database"] == "connected"
            assert data["storage"] == "connected"


@pytest.mark.asyncio
async def test_health_ready_when_degraded():
    transport = ASGITransport(app=app)
    with (
        patch("app.services.health_service.check_db_health", new_callable=AsyncMock) as mock_db,
        patch("app.storage.s3.S3StorageAdapter.check_health", new_callable=AsyncMock) as mock_s3,
    ):
        mock_db.return_value = False
        mock_s3.return_value = True

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["database"] == "disconnected"
            assert data["storage"] == "connected"
