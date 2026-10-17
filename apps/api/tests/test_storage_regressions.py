import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from app.main import app, lifespan
from app.storage.s3 import (
    BoundedStreamReader,
    FileSizeLimitExceededError,
    S3StorageAdapter,
    StorageUnavailableError,
)


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "HeadBucket")


@pytest.fixture
def adapter():
    with patch("app.storage.s3.boto3.client") as factory:
        storage = S3StorageAdapter()
    factory.return_value.get_bucket_acl.return_value = {"Grants": []}
    factory.return_value.get_bucket_policy.side_effect = client_error("NoSuchBucketPolicy")
    return storage


@pytest.mark.parametrize("code", ["403", "AccessDenied", "500"])
def test_bucket_head_errors_do_not_trigger_creation(adapter, code):
    adapter._s3_client.head_bucket.side_effect = client_error(code)
    with pytest.raises(StorageUnavailableError):
        adapter._sync_ensure_bucket()
    adapter._s3_client.create_bucket.assert_not_called()


def test_missing_bucket_creation_failure_is_propagated(adapter):
    adapter._s3_client.head_bucket.side_effect = client_error("404")
    adapter._s3_client.create_bucket.side_effect = client_error("AccessDenied")
    with pytest.raises(StorageUnavailableError):
        adapter._sync_ensure_bucket()


def test_missing_bucket_created_in_configured_region(adapter):
    adapter.region_name = "eu-west-1"
    adapter._s3_client.head_bucket.side_effect = client_error("404")
    adapter._sync_ensure_bucket()
    adapter._s3_client.create_bucket.assert_called_once_with(
        Bucket=adapter.bucket_name,
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
    )


def test_bucket_creation_race_is_verified(adapter):
    adapter._s3_client.head_bucket.side_effect = client_error("404")
    adapter._s3_client.create_bucket.side_effect = client_error("BucketAlreadyOwnedByYou")
    adapter._sync_ensure_bucket()
    adapter._s3_client.get_bucket_acl.assert_called_once()


@pytest.mark.parametrize("group", ["AllUsers", "AuthenticatedUsers"])
def test_existing_public_bucket_acl_is_rejected(adapter, group):
    adapter._s3_client.get_bucket_acl.return_value = {
        "Grants": [{"Grantee": {"Type": "Group", "URI": f"http://acs.amazonaws.com/groups/{group}"}}],
    }
    with pytest.raises(StorageUnavailableError):
        adapter._sync_ensure_bucket()


@pytest.mark.parametrize("principal", ["*", {"AWS": "*"}, {"AWS": ["*"]}])
def test_private_acl_does_not_hide_public_policy(adapter, principal):
    adapter._s3_client.get_bucket_policy.side_effect = None
    adapter._s3_client.get_bucket_policy.return_value = {
        "Policy": json.dumps({"Statement": [{"Effect": "Allow", "Principal": principal}]}),
    }
    with pytest.raises(StorageUnavailableError):
        adapter._sync_ensure_bucket()


def test_unreadable_policy_is_rejected(adapter):
    adapter._s3_client.get_bucket_policy.side_effect = client_error("AccessDenied")
    with pytest.raises(StorageUnavailableError):
        adapter._sync_ensure_bucket()


@pytest.mark.asyncio
async def test_startup_does_not_continue_after_privacy_verification_failure():
    storage = MagicMock()
    storage.ensure_bucket = AsyncMock(side_effect=StorageUnavailableError("unverified"))
    with patch("app.main.get_storage", return_value=storage):
        with pytest.raises(StorageUnavailableError):
            async with lifespan(app):
                pytest.fail("Startup accepted unverified storage")


class FragmentedStream(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        assert size >= 0, "Source must never receive an unbounded read"
        return super().read(min(size, 65536))


def test_reader_fills_short_reads_and_bounds_overflow():
    source = FragmentedStream(b"x" * 20)
    reader = BoundedStreamReader(source, max_bytes=10)
    assert reader.read(8) == b"x" * 8
    with pytest.raises(FileSizeLimitExceededError):
        reader.read()
    assert source.tell() == 11


@pytest.mark.parametrize("oversized", [False, True])
def test_actual_sdk_multipart_is_serialized_and_aborts_on_overflow(oversized):
    part_size = 8 * 1024 * 1024
    payload = b"x" * (part_size + 1)
    source = FragmentedStream(payload)
    s3 = boto3.client("s3", aws_access_key_id="testing", aws_secret_access_key="testing")
    with patch("app.storage.s3.boto3.client", return_value=s3):
        storage = S3StorageAdapter(bucket_name="testing-bucket")
    params = {"Bucket": storage.bucket_name, "Key": "testing-key"}
    original_upload_part = s3.upload_part

    def upload_part(**kwargs):
        # The first part must be sent before reading the next one. Default SDK
        # threads would queue data ahead of this request.
        if kwargs["PartNumber"] == 1:
            assert source.tell() == part_size
            assert kwargs["Body"].read() == payload[:part_size]
        return original_upload_part(**kwargs)

    with Stubber(s3) as stub, patch.object(s3, "upload_part", side_effect=upload_part):
        stub.add_response("create_multipart_upload", {"UploadId": "test-upload"})
        stub.add_response("upload_part", {"ETag": "first"})
        if oversized:
            stub.add_response("abort_multipart_upload", {})
            stub.add_response("list_multipart_uploads", {})
            stub.add_response("delete_object", {})
            with pytest.raises(FileSizeLimitExceededError):
                storage._sync_put_stream("testing-key", source, part_size)
        else:
            stub.add_response("upload_part", {"ETag": "last"})
            stub.add_response("complete_multipart_upload", {}, {
                **params,
                "UploadId": "test-upload",
                "MultipartUpload": {"Parts": [
                    {"ETag": "first", "PartNumber": 1},
                    {"ETag": "last", "PartNumber": 2},
                ]},
            })
            assert storage._sync_put_stream("testing-key", source, len(payload)) == len(payload)
        stub.assert_no_pending_responses()
