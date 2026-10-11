from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.repositories.share_repository import ShareRepository
from app.schemas.share import ShareCreateResponse
from app.services.share_service import ShareService
from app.storage.s3 import S3StorageAdapter, get_storage

router = APIRouter(prefix="/shares", tags=["Shares"])


def get_share_service(
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_storage),
) -> ShareService:
    repo = ShareRepository(db)
    return ShareService(share_repo=repo, storage=storage)


def get_canonical_base_url(request: Request) -> str:
    """Determine client-facing base URL respecting reverse proxy headers."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    return f"{proto}://{host}"


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
    base_url = get_canonical_base_url(request)
    return await service.create_share(
        file=file,
        ttl_str=ttl,
        base_url=base_url,
    )
