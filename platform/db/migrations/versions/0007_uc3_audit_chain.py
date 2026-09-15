"""Append-only, hash-chained audit tables (PRD E8).

Two additions to E8's SQL, both to make the chain well-defined rather than to
change what it stores:

- `seq` (bigserial, unique) on each chained table. E8 says `prev_hash` is "the
  previous row's hash in insertion order", and `created_at` does not give a
  total order: two rows written in one transaction share a statement timestamp.
  The chain walks `seq`.
- `created_at` is NOT NULL with no server default. The application sets it,
  because a server default would be assigned *after* the row was hashed and the
  stored hash would then describe a row that does not exist.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_uc3_audit_chain"
down_revision = "0006_uc3_calls_transcripts"
branch_labels = None
depends_on = None

CHAINED = ("analysis_runs", "flags", "dispositions")


def _chain_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False, unique=True),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("row_hash", sa.String(64), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False
        ),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("lexicon_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_chain_columns(),
    )
    op.create_index("ix_analysis_runs_call_id", "analysis_runs", ["call_id"])

    op.create_table(
        "flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("analysis_runs.id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("speaker", sa.String(64), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("evidence_span", sa.Text(), nullable=False),
        sa.Column("english_rendering", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        *_chain_columns(),
        sa.CheckConstraint("severity in ('low','medium','high')", name="ck_flags_severity"),
    )
    op.create_index("ix_flags_call_id", "flags", ["call_id"])

    op.create_table(
        "dispositions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "flag_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("flags.id"), nullable=False
        ),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.String(128), nullable=False),
        *_chain_columns(),
        sa.CheckConstraint(
            "disposition in ('confirmed','false_positive','needs_more_context','escalated')",
            name="ck_dispositions_disposition",
        ),
    )
    op.create_index("ix_dispositions_flag_id", "dispositions", ["flag_id"])


def downgrade() -> None:
    for table in reversed(CHAINED):
        op.drop_table(table)
