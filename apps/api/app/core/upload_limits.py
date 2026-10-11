"""Bound multipart spooling before FastAPI resolves upload parameters."""

from starlette.formparsers import MultiPartException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class UploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, upload_path: str, max_body_bytes: int) -> None:
        self.app = app
        self.upload_path = upload_path
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] not in {
            self.upload_path, f"{self.upload_path}/",
        }:
            await self.app(scope, receive, send)
            return

        received = 0
        exceeded = False
        rejection_sent = False

        async def bounded_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    # Starlette closes partially spooled files on this exception,
                    # including versions that only catch MultiPartException.
                    raise MultiPartException("Upload request body exceeds limit")
            return message

        async def bounded_send(message: Message) -> None:
            nonlocal rejection_sent
            if not exceeded:
                await send(message)
            elif not rejection_sent:
                rejection_sent = True
                # Replace the multipart parser's 400 with the size-limit contract.
                response = JSONResponse(
                    {"detail": "Upload request body exceeds allowed size"},
                    status_code=413,
                )
                await response(scope, receive, send)

        await self.app(scope, bounded_receive, bounded_send)
