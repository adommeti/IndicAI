"""Persist chat turns without changing the shipped sessions migration."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_helpdesk_turns"
down_revision = "0001_shared"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("utterance", sa.Text(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        *[
            sa.Column(name, JSONB(), nullable=False)
            for name in ("decision_json", "retrieval_json", "latency_ms")
        ],
        *[
            sa.Column(name, sa.String(64), nullable=False)
            for name in ("policy_version", "prompt_version", "trace_id")
        ],
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_turns_session_id", "turns", ["session_id"])


def downgrade() -> None:
    op.drop_table("turns")
