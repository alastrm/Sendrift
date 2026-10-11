import asyncio
import logging
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class S3StorageAdapter:
    """Adapter for private S3-compatible object storage (MinIO/AWS S3)."""

    def __init__(
        self,
        endpoint_url: str = settings.S3_ENDPOINT_URL,
        access_key: str = settings.S3_ACCESS_KEY,
        secret_key: str = settings.S3_SECRET_KEY,
        bucket_name: str = settings.S3_BUCKET_NAME,
        region_name: str = settings.S3_REGION,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket_name = bucket_name
        self._s3_client: Any = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def _sync_check_health(self) -> bool:
        """Synchronously check if storage is accessible without leaking secrets."""
        try:
            self._s3_client.head_bucket(Bucket=self.bucket_name)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            # If bucket doesn't exist yet, but client connected, or if 404
            logger.warning("S3 health check returned ClientError code: %s", error_code)
            return False
        except Exception as exc:
            logger.error("S3 health check unexpected error: %s", type(exc).__name__)
            return False

    async def check_health(self) -> bool:
        """Asynchronously check storage reachability via threadpool."""
        return await asyncio.to_thread(self._sync_check_health)


_storage_singleton: S3StorageAdapter | None = None


def get_storage() -> S3StorageAdapter:
    """Dependency provider for storage adapter."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = S3StorageAdapter()
    return _storage_singleton
