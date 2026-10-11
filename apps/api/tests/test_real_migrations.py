"""Migration checks use a disposable schema in the opted-in PostgreSQL database."""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.models.share import Share, ShareStatus

pytestmark = pytest.mark.skipif(
    os.environ.get("SENDRIFT_REAL_INTEGRATION") != "1",
    reason="Set SENDRIFT_REAL_INTEGRATION=1 to use disposable PostgreSQL/MinIO infrastructure",
)


def test_fresh_and_existing_database_migrations(monkeypatch):
    schema = f"migration_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(settings.sync_database_url, connect_args={"connect_timeout": 5})
    schema_engine = None
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        url = make_url(settings.sync_database_url).update_query_dict({
            "options": f"-csearch_path={schema}",
        })
        monkeypatch.setattr(
            settings, "DATABASE_SYNC_URL", url.render_as_string(hide_password=False),
        )
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(config, "head")
        schema_engine = create_engine(url, connect_args={"connect_timeout": 5})
        with schema_engine.begin() as connection:
            connection.execute(insert(Share).values(
                share_token_hash="a" * 64,
                manage_token_hash="b" * 64,
                object_key="migration-probe",
                original_filename="probe.bin",
                ttl_seconds=3600,
                status=ShareStatus.UPLOADING,
            ))
        # Already-stamped databases must preserve both data and lowercase enum values.
        command.upgrade(config, "head")
        with schema_engine.connect() as connection:
            assert connection.execute(select(Share.status)).scalar_one() == ShareStatus.UPLOADING
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        command.check(config)
        command.downgrade(config, "base")
        with schema_engine.connect() as connection:
            enum_type = connection.execute(text("SELECT to_regtype('share_status')")).scalar_one()
            assert enum_type is None
        command.upgrade(config, "head")
        command.check(config)
    finally:
        if schema_engine is not None:
            schema_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
