"""Calls and diarized transcript segments (PRD E8, plus a Roman rendering).

Two deviations from the E8 SQL, both deliberate and both recorded in the uc3/P2
report:

- `calls.source_key` (unique) and `calls.stt_cost_inr` / `stt_model` are added.
  The unique key is what makes the nightly sweep idempotent -- without it, two
  runs over the same prefix create two calls for one recording -- and per-call
  STT cost is a PRD B6 number that cannot be reconstructed later.
- `transcript_segments.text_roman` / `roman_source` are added. Lexicon matching
  (P3) has to see Hinglish in both scripts; `text` stays authoritative and is
  what evidence spans are quoted from.

`participants` and `languages` are JSONB rather than E8's `text[]`: the rest of
this schema uses JSONB for list columns, and mixing the two would mean two query
idioms for the same shape.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_uc3_calls_transcripts"
down_revision = "0005_uc2_quiz_attempt_cohort"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_key", sa.String(512), nullable=False, unique=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False),
        sa.Column(
            "participants",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "languages",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stt_cost_inr", sa.Numeric(12, 4), nullable=False),
        sa.Column("stt_model", sa.String(64), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('pending','transcribing','transcribed','failed')",
            name="ck_calls_status",
        ),
        sa.CheckConstraint("duration_s >= 0", name="ck_calls_duration"),
    )
    op.create_table(
        "transcript_segments",
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            primary_key=True,
        ),
        sa.Column("seg_id", sa.Integer(), primary_key=True),
        sa.Column("speaker", sa.String(64), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_roman", sa.Text(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("roman_source", sa.String(64), nullable=False),
        sa.CheckConstraint("end_ms >= start_ms", name="ck_transcript_segments_span"),
    )
    op.create_index("ix_transcript_segments_call", "transcript_segments", ["call_id", "start_ms"])


def downgrade() -> None:
    op.drop_index("ix_transcript_segments_call", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_table("calls")
