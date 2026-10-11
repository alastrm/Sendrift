import asyncio
import io
import logging
from typing import Any, BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base exception for storage errors."""
    pass


class FileSizeLimitExceededError(StorageError):
    """Raised when file exceeds the maximum allowed byte limit."""
    pass


class EmptyFileError(StorageError):
    """Raised when the uploaded file is 0 bytes."""
    pass


class StorageUnavailableError(StorageError):
    """Raised when S3/MinIO service is unavailable."""
    pass


class BoundedStreamReader(io.RawIOBase):
    """Wrapper around binary stream that counts bytes and enforces maximum size limit."""

    def __init__(self, source_stream: BinaryIO, max_bytes: int) -> None:
        super().__init__()
        self.source_stream = source_stream
        self.max_bytes = max_bytes
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self.source_stream.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            if self.bytes_read > self.max_bytes:
                raise FileSizeLimitExceededError(
                    f"File exceeds maximum allowed limit of {self.max_bytes} bytes"
                )
        return chunk

    def readinto(self, b: bytearray) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n


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
            logger.warning("S3 health check returned ClientError code: %s", error_code)
            return False
        except Exception as exc:
            logger.error("S3 health check unexpected error: %s", type(exc).__name__)
            return False

    async def check_health(self) -> bool:
        """Asynchronously check storage reachability via threadpool."""
        return await asyncio.to_thread(self._sync_check_health)

    def _sync_put_stream(
        self,
        object_key: str,
        stream: BinaryIO,
        max_size_bytes: int,
        content_type: str | None = None,
    ) -> int:
        """Synchronously upload stream in bounded chunks and return actual bytes read."""
        bounded_reader = BoundedStreamReader(stream, max_bytes=max_size_bytes)
        extra_args: dict[str, str] = {
            "ContentType": content_type or "application/octet-stream",
        }

        try:
            self._s3_client.upload_fileobj(
                Fileobj=bounded_reader,
                Bucket=self.bucket_name,
                Key=object_key,
                ExtraArgs=extra_args,
            )

            if bounded_reader.bytes_read == 0:
                self._sync_delete_object(object_key)
                raise EmptyFileError("File is empty (0 bytes)")

            return bounded_reader.bytes_read

        except FileSizeLimitExceededError:
            self._sync_delete_object(object_key)
            raise

        except EmptyFileError:
            raise

        except (ClientError, EndpointConnectionError) as exc:
            logger.error("S3 client/endpoint error during upload: %s", type(exc).__name__)
            self._sync_delete_object(object_key)
            raise StorageUnavailableError("Object storage service is unavailable") from exc

        except Exception as exc:
            logger.error("Unexpected error during S3 upload: %s", type(exc).__name__)
            self._sync_delete_object(object_key)
            raise StorageError(f"Failed to store file object: {type(exc).__name__}") from exc

    async def put_stream(
        self,
        object_key: str,
        stream: BinaryIO,
        max_size_bytes: int,
        content_type: str | None = None,
    ) -> int:
        """Asynchronously stream upload file to private bucket with bounded size limit."""
        return await asyncio.to_thread(
            self._sync_put_stream,
            object_key,
            stream,
            max_size_bytes,
            content_type,
        )

    def _sync_delete_object(self, object_key: str) -> bool:
        """Synchronously delete an object; idempotent."""
        try:
            self._s3_client.delete_object(Bucket=self.bucket_name, Key=object_key)
            return True
        except Exception as exc:
            logger.warning("Failed to delete object: %s", type(exc).__name__)
            return False

    async def delete_object(self, object_key: str) -> bool:
        """Asynchronously delete an object via threadpool."""
        return await asyncio.to_thread(self._sync_delete_object, object_key)


_storage_singleton: S3StorageAdapter | None = None


def get_storage() -> S3StorageAdapter:
    """Dependency provider for storage adapter."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = S3StorageAdapter()
    return _storage_singleton
