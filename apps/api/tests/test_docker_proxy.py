"""Opt-in smoke tests of the built Compose stack; only remove their own fixtures.

Run after `docker compose up -d --build` with SENDRIFT_DOCKER_SMOKE=1.
Real tokens stay in memory/stdin, never command arguments or test output.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SENDRIFT_DOCKER_SMOKE") != "1",
    reason="Set SENDRIFT_DOCKER_SMOKE=1 against the running local Compose stack",
)
ROOT = Path(__file__).resolve().parents[3]
BASE = "http://localhost:3000"

# Execute inside the API container to use its real DB/S3 configuration without
# exporting credentials. Only synthetic fixture metadata is sent over stdin.
PROBE = """
import hashlib, json, sys
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import create_engine, text
from app.core.config import settings
data = json.load(sys.stdin)
engine = create_engine(settings.sync_database_url)
with engine.connect() as connection:
    row = connection.execute(text('''
        SELECT object_key, status::text AS status, size_bytes, manage_token_hash, download_count
        FROM shares WHERE share_token_hash = :token_hash
    '''), {'token_hash': hashlib.sha256(data['token'].encode()).hexdigest()}).mappings().first()
client = boto3.client('s3', endpoint_url=settings.S3_ENDPOINT_URL,
    aws_access_key_id=settings.S3_ACCESS_KEY, aws_secret_access_key=settings.S3_SECRET_KEY)
key = row['object_key'] if row else data['key']
if data.get('deleted'):
    try:
        client.head_object(Bucket=settings.S3_BUCKET_NAME, Key=key)
        absent = False
    except ClientError as error:
        absent = error.response['ResponseMetadata']['HTTPStatusCode'] == 404
    result = {'gone': row is None and absent}
else:
    digest = hashlib.sha256()
    body = client.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)['Body']
    try:
        for chunk in body.iter_chunks(chunk_size=65536):
            digest.update(chunk)
    finally:
        body.close()
    anonymous = boto3.client('s3', endpoint_url=settings.S3_ENDPOINT_URL,
        config=Config(signature_version=UNSIGNED))
    try:
        anonymous.get_object(Bucket=settings.S3_BUCKET_NAME, Key=key)['Body'].close()
        private = False
    except ClientError as error:
        private = error.response['ResponseMetadata']['HTTPStatusCode'] == 403
    finally:
        anonymous.close()
    result = {'key': key, 'ok': (
        row['status'] == 'ready' and row['size_bytes'] == data['size']
        and row['download_count'] >= 1 and digest.hexdigest() == data['digest']
        and row['manage_token_hash'] == hashlib.sha256(data['manage'].encode()).hexdigest()
        and private
    )}
client.close()
engine.dispose()
print(json.dumps(result))
"""


def docker(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=ROOT, input=input_text,
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, "Docker verification command failed"
    return result.stdout + result.stderr


def probe(data: dict) -> dict:
    return json.loads(docker("exec", "-i", "sendrift-api", "python", "-c", PROBE,
                             input_text=json.dumps(data)))


def test_built_docker_proxy_and_worker_configuration():
    manifest = json.loads(docker(
        "exec", "sendrift-web", "node", "-e",
        "console.log(JSON.stringify(require('./.next/routes-manifest.json').rewrites))",
    ))
    assert {route["destination"] for route in manifest} == {
        "http://api:8000/api/:path*", "http://api:8000/health/:path*",
    }
    worker = json.loads(docker("inspect", "sendrift-worker"))[0]
    assert worker["State"]["Running"]
    assert worker["Config"]["Healthcheck"]["Test"] == ["NONE"]
    with httpx.Client(base_url=BASE, trust_env=False) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["database"] == "connected"
        assert response.json()["storage"] == "connected"


def test_real_proxy_roundtrip_and_worker_cleanup():
    started = datetime.now(timezone.utc).isoformat()
    fixtures: list[dict] = []
    with httpx.Client(base_url=BASE, timeout=120, trust_env=False) as client:
        try:
            for size in (1, 1024 * 1024, 100 * 1024 * 1024):
                digest = hashlib.sha256()
                block = b"Sendrift synthetic Docker proxy fixture\x00\xff" * 2048
                with tempfile.TemporaryFile() as source:
                    remaining = size
                    while remaining:
                        chunk = block[:remaining]
                        source.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    source.seek(0)
                    response = client.post(
                        "/api/v1/shares", data={"ttl": "1h"},
                        files={"file": ("docker-proxy.bin", source, "application/octet-stream")},
                        headers={"X-Forwarded-Host": "attacker.example",
                                 "X-Forwarded-Proto": "javascript"},
                    )
                assert response.status_code == 201
                data = response.json()
                fixture = {"token": data["share_url"].rsplit("/", 1)[-1],
                           "manage": data["manage_token"], "size": size,
                           "digest": digest.hexdigest()}
                fixtures.append(fixture)
                assert data["share_url"] == f"{BASE}/s/{fixture['token']}"
                assert client.get(data["share_url"]).status_code == 200
                metadata = client.get(f"/api/v1/shares/{fixture['token']}")
                assert metadata.status_code == 200
                assert metadata.json()["size_bytes"] == size
                downloaded = hashlib.sha256()
                with client.stream("GET", f"/api/v1/shares/{fixture['token']}/download") as stream:
                    assert stream.status_code == 200
                    for chunk in stream.iter_bytes():
                        downloaded.update(chunk)
                assert downloaded.hexdigest() == digest.hexdigest()
                stored = probe(fixture)
                assert stored["ok"], (
                    "DB state, stored bytes, hashed management token or privacy failed"
                )
                fixture["key"] = stored["key"]

            for service in ("sendrift-api", "sendrift-web"):
                logs = docker("logs", "--since", started, service)
                for fixture in fixtures:
                    assert fixture["token"] not in logs, "Share token leaked in container logs"
                    assert fixture["manage"] not in logs, (
                        "Management token leaked in container logs"
                    )
        finally:
            for fixture in fixtures:
                response = client.delete(
                    f"/api/v1/shares/{fixture['token']}",
                    headers={"X-Manage-Token": fixture["manage"]},
                )
                assert response.status_code in (202, 404)

        # Verify the actual background worker, without manually running a cleanup pass.
        deadline = time.monotonic() + 90
        while True:
            gone = [probe({**fixture, "deleted": True})["gone"] for fixture in fixtures]
            if all(gone):
                break
            assert time.monotonic() < deadline, "Worker did not remove revoked fixture rows/objects"
            time.sleep(2)
