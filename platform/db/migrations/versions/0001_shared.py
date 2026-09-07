"""Shared sessions and metadata-only adapter calls."""

import sqlalchemy as sa
from alembic import op

revision = "0001_shared"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("app", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "adapter_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id")),
        sa.Column("vendor", sa.String(32), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("cost_inr", sa.Numeric(18, 8), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_adapter_calls_session_id", "adapter_calls", ["session_id"])


def downgrade() -> None:
    op.drop_table("adapter_calls")
    op.drop_table("sessions")
