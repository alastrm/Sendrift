from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.repositories.share_repository import ShareRepository
from app.schemas.share import ShareCreateResponse, ShareMetadataResponse
from app.services.share_service import ShareService, format_content_disposition
from app.storage.s3 import S3StorageAdapter, get_storage

router = APIRouter(prefix="/shares", tags=["Shares"])


def get_share_service(
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> ShareService:
    repo = ShareRepository(db)
    return ShareService(share_repo=repo, storage=storage)


def get_canonical_base_url() -> str:
    """Use deployment configuration, never client-controlled Host/Forwarded headers."""
    return str(settings.PUBLIC_WEB_URL).rstrip("/")


@router.post(
    "",
    response_model=ShareCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and share a single file",
    description="Uploads one file (1 byte to 100 MiB) with specified TTL (1h, 24h, 7d).",
)
async def create_share(
    request: Request,
    file: UploadFile = File(..., description="Single file to share (1 to 104857600 bytes)"),
    ttl: str = Form(..., description="Retention TTL: '1h', '24h', or '7d'"),
    service: ShareService = Depends(get_share_service),
) -> ShareCreateResponse:
    form = await request.form()
    if len(form.getlist("file")) != 1 or len(form.getlist("ttl")) != 1 or set(form) != {
        "file", "ttl",
    }:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Exactly one file and one TTL are required")
    base_url = get_canonical_base_url()
    return await service.create_share(
        file=file,
        ttl_str=ttl,
        base_url=base_url,
    )


@router.get(
    "/{share_token}",
    response_model=ShareMetadataResponse,
    status_code=status.HTTP_200_OK,
    summary="Get share metadata",
    description="Returns public metadata if share exists, is ready, and unexpired.",
)
async def get_share_metadata(
    share_token: str,
    service: ShareService = Depends(get_share_service),
) -> ShareMetadataResponse:
    return await service.get_share_metadata(share_token)


@router.get(
    "/{share_token}/download",
    status_code=status.HTTP_200_OK,
    summary="Download shared file",
    description="Streams file bytes as attachment from private storage with security headers.",
)
@router.head(
    "/{share_token}/download",
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def download_share(
    share_token: str,
    request: Request,
    service: ShareService = Depends(get_share_service),
) -> Response:
    is_head = request.method == "HEAD"
    share, stream = await service.get_share_for_download(
        share_token=share_token,
        is_head_request=is_head,
    )

    headers = {
        "Content-Disposition": format_content_disposition(share.original_filename),
        "Content-Type": "application/octet-stream",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }
    if share.size_bytes is not None:
        headers["Content-Length"] = str(share.size_bytes)

    if is_head:
        await stream.close()
        return Response(status_code=status.HTTP_200_OK, headers=headers)

    from starlette.background import BackgroundTask

    return StreamingResponse(
        stream.iter_bytes(chunk_size=65536),
        status_code=status.HTTP_200_OK,
        media_type="application/octet-stream",
        headers=headers,
        background=BackgroundTask(stream.close),
    )


@router.delete(
    "/{share_token}",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Revoke share access and schedule deletion",
    description=(
        "Validates owner secret, revokes access immediately, and schedules physical deletion."
    ),
)
async def delete_share(
    share_token: str,
    x_manage_token: str | None = Header(None, alias="X-Manage-Token"),
    service: ShareService = Depends(get_share_service),
) -> dict[str, str]:
    if not x_manage_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found or expired",
        )
    return await service.revoke_share(share_token=share_token, manage_token=x_manage_token)
