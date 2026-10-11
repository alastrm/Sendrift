from tempfile import SpooledTemporaryFile
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.api.v1.shares import get_share_service
from app.core.upload_limits import UploadBodyLimitMiddleware
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
async def test_stream_limit_closes_spooled_files_with_absent_or_forged_length(monkeypatch, headers):
    files = []

    def tracking_spool(*args, **kwargs):
        spool = SpooledTemporaryFile(*args, **kwargs)
        files.append(spool)
        return spool

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracking_spool)
    test_app = FastAPI()

    @test_app.post("/upload")
    async def upload(request: Request):
        await request.form()
        pytest.fail("Oversized multipart reached the handler")

    bounded_app = UploadBodyLimitMiddleware(test_app, "/upload", 256)

    async def body():
        yield (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.bin"'
            b"\r\n\r\n" + b"x" * 100
        )
        yield b"x" * 200
        pytest.fail("Request consumed more data after exceeding the limit")

    transport = ASGITransport(app=bounded_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/upload",
            content=body(),
            headers={"Content-Type": "multipart/form-data; boundary=boundary", **headers},
        )
    assert response.status_code == 413
    assert files and all(file.closed for file in files)


@pytest.mark.asyncio
async def test_duplicate_files_are_rejected_before_storage():
    service = AsyncMock()
    app.dependency_overrides[get_share_service] = lambda: service
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/shares",
                files=[("file", ("a.bin", b"a")), ("file", ("b.bin", b"b"))],
                data={"ttl": "1h"},
            )
        assert response.status_code == 400
        service.create_share.assert_not_awaited()
    finally:
        app.dependency_overrides.pop(get_share_service, None)
