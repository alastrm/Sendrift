"""Storage adapter package."""
from app.storage.s3 import S3StorageAdapter, get_storage

__all__ = ["S3StorageAdapter", "get_storage"]
