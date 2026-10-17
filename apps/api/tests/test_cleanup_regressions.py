import asyncio
import io
import sys
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.stub import Stubber

from app.repositories.share_repository import ShareRepository
from app.services.cleanup_service import CleanupResult, CleanupService
from app.storage.s3 import S3StorageAdapter, StorageUnavailableError
from app.worker.cleanup import main, run_worker_loop


@pytest.mark.asyncio
async def test_orphan_audit_reaches_second_s3_page(monkeypatch):
    storage = S3StorageAdapter()
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)
    known = [f"known-{i:04d}" for i in range(1000)]
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    repo = MagicMock(spec=ShareRepository)
    repo.get_existing_object_keys = AsyncMock(side_effect=[set(known), set(), set()])
    monkeypatch.setattr("app.services.cleanup_service.ShareRepository", lambda _: repo)
    service = CleanupService(session_factory=lambda: session, storage=storage)
    with Stubber(storage._s3_client) as stub:
        stub.add_response("list_objects_v2", {
            "IsTruncated": True, "NextContinuationToken": "page-two",
            "Contents": [{"Key": key, "LastModified": old} for key in known],
        }, {"Bucket": storage.bucket_name, "MaxKeys": 1000})
        stub.add_response("list_objects_v2", {
            "IsTruncated": False, "Contents": [{"Key": "old-orphan", "LastModified": old}],
        }, {"Bucket": storage.bucket_name, "MaxKeys": 1000, "ContinuationToken": "page-two"})
        stub.add_response("list_multipart_uploads", {}, {
            "Bucket": storage.bucket_name, "Prefix": "old-orphan",
        })
        stub.add_response("delete_object", {}, {"Bucket": storage.bucket_name, "Key": "old-orphan"})
        stub.add_response("list_multipart_uploads", {}, {
            "Bucket": storage.bucket_name, "MaxUploads": 1000,
        })
        result = CleanupResult()
        await service._reconcile_orphan_objects(result)
        assert result.orphan_s3_objects_cleaned == 1
        assert result.storage_errors == 0
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_orphan_audit_rechecks_database_before_deletion(monkeypatch):
    storage = MagicMock(spec=S3StorageAdapter)

    async def batches(**kwargs):
        yield ["newly-tracked"]

    storage.iter_object_key_batches = batches
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock()
    repo = MagicMock(spec=ShareRepository)
    repo.get_existing_object_keys = AsyncMock(side_effect=[set(), {"newly-tracked"}])
    monkeypatch.setattr("app.services.cleanup_service.ShareRepository", lambda _: repo)
    result = CleanupResult()
    await CleanupService(lambda: session, storage)._reconcile_orphan_objects(result)
    storage.delete_object.assert_not_called()
    assert result.orphan_s3_objects_cleaned == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_error", [None, "NoSuchUpload", "AccessDenied"])
async def test_deletion_aborts_only_exact_key_and_retains_failure(abort_error):
    storage = S3StorageAdapter()
    with Stubber(storage._s3_client) as stub:
        stub.add_response("list_multipart_uploads", {"Uploads": [
            {"Key": "target-neighbor", "UploadId": "neighbor-upload"},
            {"Key": "target", "UploadId": "target-upload"},
        ]}, {"Bucket": storage.bucket_name, "Prefix": "target"})
        abort_params = {
            "Bucket": storage.bucket_name, "Key": "target", "UploadId": "target-upload",
        }
        if abort_error:
            stub.add_client_error(
                "abort_multipart_upload", service_error_code=abort_error,
                expected_params=abort_params,
            )
        else:
            stub.add_response("abort_multipart_upload", {}, abort_params)
        if abort_error != "AccessDenied":
            stub.add_response("delete_object", {}, {"Bucket": storage.bucket_name, "Key": "target"})
        assert await storage.delete_object("target") == (abort_error != "AccessDenied")
        stub.assert_no_pending_responses()


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_actual_sdk_thread(monkeypatch):
    storage = S3StorageAdapter()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def delayed_put(*args):
        entered.set()
        try:
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release SDK thread")
            return 1
        finally:
            finished.set()

    monkeypatch.setattr(storage, "_sync_put_stream", delayed_put)
    transfer = asyncio.create_task(storage.put_stream("test-key", io.BytesIO(b"x"), 1))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        transfer.cancel()
        await asyncio.sleep(0)
        assert not transfer.done()
        transfer.cancel()  # Repeated cancellation must not free the upload's DB lock either.
        await asyncio.sleep(0)
        assert not transfer.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await transfer
    assert finished.is_set()


@pytest.mark.asyncio
async def test_worker_default_cli_enters_loop(monkeypatch):
    loop = AsyncMock()
    monkeypatch.setattr(sys, "argv", ["app.worker.cleanup"])
    monkeypatch.setattr("app.worker.cleanup.run_worker_loop", loop)
    # Avoid changing signal handlers in the pytest process.
    monkeypatch.setattr("app.worker.cleanup.signal.signal", lambda *args: None)
    await main()
    loop.assert_awaited_once()
    assert not loop.call_args.kwargs["reconcile_orphans"]


@pytest.mark.asyncio
async def test_worker_retries_after_failure_without_logging_exception_details(monkeypatch, caplog):
    stop = asyncio.Event()
    cycles = 0

    async def cycle(**kwargs):
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            raise StorageUnavailableError("sensitive-error-marker")
        stop.set()
        return CleanupResult()

    monkeypatch.setattr("app.worker.cleanup.run_cleanup_cycle", cycle)
    await asyncio.wait_for(run_worker_loop(interval_seconds=0.001, stop_event=stop), 2)
    assert cycles == 2
    assert "sensitive-error-marker" not in caplog.text
    assert "StorageUnavailableError" in caplog.text
