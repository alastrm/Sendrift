from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.v1.shares import get_share_service
from app.core.config import Settings, settings
from app.main import app
from app.schemas.share import ShareCreateResponse


@pytest.mark.parametrize("origin", [
    "javascript:alert(1)", "ftp://files.example", "https://user:password@files.example",
    "https://files.example/path", "https://files.example/?query=1",
    "https://files.example/#fragment", "https://files.example:99999",
])
def test_public_web_url_requires_http_origin(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, PUBLIC_WEB_URL=origin)


@pytest.mark.parametrize("origin", [
    "http://localhost:3000", "https://sendrift.example", "http://[::1]:3000/",
])
def test_public_web_url_accepts_deployment_origins(origin):
    assert str(Settings(_env_file=None, PUBLIC_WEB_URL=origin).PUBLIC_WEB_URL).rstrip("/") == (
        origin.rstrip("/")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"X-Forwarded-Host": "attacker.example", "X-Forwarded-Proto": "https"},
    {"X-Forwarded-Host": "good.example, attacker.example", "X-Forwarded-Proto": "javascript"},
    {"Forwarded": 'for=127.0.0.1;host="attacker.example";proto=https'},
])
async def test_upload_and_redirect_ignore_untrusted_origin_headers(monkeypatch, headers):
    origin = Settings(_env_file=None, PUBLIC_WEB_URL="https://sendrift.example").PUBLIC_WEB_URL
    monkeypatch.setattr(settings, "PUBLIC_WEB_URL", origin)

    async def create_share(*, file, ttl_str, base_url):
        return ShareCreateResponse(
            share_url=f"{base_url}/s/fixture-token",
            manage_token="fixture-management-key",
            filename=file.filename,
            size_bytes=1,
            expires_at=datetime.now(timezone.utc),
        )

    service = AsyncMock()
    service.create_share.side_effect = create_share
    monkeypatch.setitem(app.dependency_overrides, get_share_service, lambda: service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api:8000") as client:
        response = await client.post(
            "/api/v1/shares", headers=headers,
            files={"file": ("fixture.txt", b"x")}, data={"ttl": "1h"},
        )
        assert response.status_code == 201
        assert response.json()["share_url"] == "https://sendrift.example/s/fixture-token"
        redirect = await client.get("/s/fixture-token", headers=headers)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "https://sendrift.example/s/fixture-token"
