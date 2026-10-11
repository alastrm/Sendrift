import asyncio
import io
import json
import logging
from typing import Any, BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
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
        # Never read past the limit except for the one byte needed to detect overflow.
        remaining = self.max_bytes - self.bytes_read + 1
        requested = remaining if size < 0 else min(size, remaining)
        chunks: list[bytes] = []
        # The transfer SDK treats a short read as EOF when deciding between PUT
        # and multipart. Fill the request even when the underlying stream fragments it.
        while requested > 0:
            chunk = self.source_stream.read(requested)
            if not chunk:
                break
            self.bytes_read += len(chunk)
            if self.bytes_read > self.max_bytes:
                raise FileSizeLimitExceededError(
                    f"File exceeds maximum allowed limit of {self.max_bytes} bytes"
                )
            chunks.append(chunk)
            requested -= len(chunk)
        return b"".join(chunks)

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
        self.region_name = region_name
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

    def _sync_ensure_bucket(self) -> None:
        """Create a missing bucket and reject unverifiable or public access controls."""
        try:
            try:
                self._s3_client.head_bucket(Bucket=self.bucket_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket"}:
                    raise
                create_args: dict[str, Any] = {"Bucket": self.bucket_name}
                if self.region_name != "us-east-1":
                    create_args["CreateBucketConfiguration"] = {
                        "LocationConstraint": self.region_name,
                    }
                try:
                    self._s3_client.create_bucket(**create_args)
                except ClientError as create_exc:
                    # Another API process may have created the bucket concurrently.
                    code = create_exc.response.get("Error", {}).get("Code")
                    if code != "BucketAlreadyOwnedByYou":
                        raise

            acl = self._s3_client.get_bucket_acl(Bucket=self.bucket_name)
            for grant in acl["Grants"]:
                grantee = grant.get("Grantee", {})
                if grantee.get("Type") == "Group" and grantee.get("URI", "").endswith(
                    ("/AllUsers", "/AuthenticatedUsers")
                ):
                    raise StorageError("Bucket has public ACL grants")

            try:
                policy_response = self._s3_client.get_bucket_policy(Bucket=self.bucket_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NoSuchBucketPolicy":
                    raise
            else:
                policy = json.loads(policy_response["Policy"])
                statements = policy.get("Statement", [])
                if isinstance(statements, dict):
                    statements = [statements]
                for statement in statements:
                    if statement.get("Effect") != "Allow":
                        continue
                    # Conservatively reject broad principals, including conditional
                    # grants. A private ACL does not override a public bucket policy.
                    principal = statement.get("Principal", {})
                    values = principal.values() if isinstance(principal, dict) else [principal]
                    if "NotPrincipal" in statement or any(
                        "*" in value
                        for entry in values
                        for value in (entry if isinstance(entry, list) else [entry])
                    ):
                        raise StorageError("Bucket policy allows public access")
        except Exception as exc:
            logger.error("Bucket privacy verification failed: %s", type(exc).__name__)
            raise StorageUnavailableError("Private storage verification failed") from None

    async def ensure_bucket(self) -> None:
        """Asynchronously ensure private bucket exists via threadpool."""
        await asyncio.to_thread(self._sync_ensure_bucket)

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
                # Serialize SDK transfers inside the existing worker thread. The
                # default in-memory multipart queue can otherwise hold most of a file.
                Config=TransferConfig(
                    multipart_threshold=8 * 1024 * 1024,
                    multipart_chunksize=8 * 1024 * 1024,
                    use_threads=False,
                    preferred_transfer_client="classic",
                ),
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
