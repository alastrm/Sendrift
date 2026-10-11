"""create shares table

Revision ID: 0001
Revises: None
Create Date: 2026-10-11 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

share_status_enum = sa.Enum(
    "uploading", "ready", "failed", "deleting", name="share_status"
)


def upgrade() -> None:
    op.create_table(
        "shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("share_token_hash", sa.String(64), nullable=False),
        sa.Column("manage_token_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            share_status_enum,
            nullable=False,
            server_default="uploading",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "download_count",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ttl_seconds IN (3600, 86400, 604800)",
            name="ck_shares_ttl_seconds",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR (size_bytes >= 1 AND size_bytes <= 104857600)",
            name="ck_shares_size_bytes",
        ),
    )
    op.create_index(
        "ix_shares_share_token_hash",
        "shares",
        ["share_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_shares_manage_token_hash",
        "shares",
        ["manage_token_hash"],
        unique=False,
    )
    op.create_index(
        "ix_shares_object_key",
        "shares",
        ["object_key"],
        unique=True,
    )
    op.create_index(
        "ix_shares_expires_at",
        "shares",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_shares_expires_at", table_name="shares")
    op.drop_index("ix_shares_object_key", table_name="shares")
    op.drop_index("ix_shares_manage_token_hash", table_name="shares")
    op.drop_index("ix_shares_share_token_hash", table_name="shares")
    op.drop_table("shares")
    share_status_enum.drop(op.get_bind(), checkfirst=True)
