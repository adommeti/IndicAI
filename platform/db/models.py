import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    String,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    app: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AdapterCall(Base):
    __tablename__ = "adapter_calls"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"), index=True)
    vendor: Mapped[str] = mapped_column(String(32))
    capability: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[float]
    units: Mapped[dict[str, Any]] = mapped_column(JSON)
    cost_inr: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), index=True)
    utterance: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16))
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    retrieval_json: Mapped[dict[str, Any]] = mapped_column(JSONB)
    latency_ms: Mapped[dict[str, Any]] = mapped_column(JSONB)
    policy_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    trace_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- UC2 training localizer (PRD D6) -----------------------------------------


class Module(Base):
    __tablename__ = "modules"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text)
    source_lang: Mapped[str] = mapped_column(String(16), default="en-IN")
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Segment(Base):
    __tablename__ = "segments"
    module_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("modules.id"), primary_key=True)
    seg_id: Mapped[int] = mapped_column(primary_key=True)
    start_ms: Mapped[int]
    end_ms: Mapped[int]
    source_text: Mapped[str] = mapped_column(Text)
    locked: Mapped[bool] = mapped_column(default=False)


class Localization(Base):
    """One stage's output for one segment in one language, versioned.

    The primary key is what makes a stage re-runnable on its own: a re-run
    writes a new `version` rather than overwriting, so a reviewer edit
    triggers re-production and never re-translation (PRD D5).
    """

    __tablename__ = "localizations"
    # Declared here as well as in 0003_uc2_modules so `alembic check` does not
    # see the migration's index as drift and autogenerate a drop.
    __table_args__ = (Index("ix_localizations_module_language", "module_id", "language", "stage"),)
    module_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("modules.id"), primary_key=True)
    seg_id: Mapped[int] = mapped_column(primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    # rationale, change_log, qa_score, glossary_hits, glossary_version, model,
    # prompt_version — every persisted row records what produced it.
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizItem(Base):
    __tablename__ = "quiz_items"
    module_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("modules.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    item_id: Mapped[int] = mapped_column(primary_key=True)
    seg_id: Mapped[int]
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSONB)
    answer: Mapped[int]
    rationale: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(default=False)


class Artifact(Base):
    __tablename__ = "artifacts"
    module_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("modules.id"), primary_key=True)
    language: Mapped[str] = mapped_column(String(16), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    # Measurements that belong to this artifact: timing-fit from the dub's SRT
    # export, the versions that produced it, job ids. Added in 0004.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    module_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modules.id"), index=True
    )  # ix_quiz_attempts_module_id
    language: Mapped[str] = mapped_column(String(16))
    employee_id: Mapped[str] = mapped_column(String(128))
    score: Mapped[int]
    max_score: Mapped[int]
    # D10 needs the arm and the time-on-task, neither recoverable afterwards. 0005.
    cohort: Mapped[str] = mapped_column(String(16), default="native", server_default="native")
    duration_ms: Mapped[int] = mapped_column(default=0, server_default="0")
    pilot_id: Mapped[str] = mapped_column(
        String(64), default="unassigned", server_default="unassigned"
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("cohort in ('native','control')", name="ck_quiz_attempts_cohort"),
        CheckConstraint("score >= 0 and score <= max_score", name="ck_quiz_attempts_score"),
        Index("ix_quiz_attempts_report", "pilot_id", "language", "cohort"),
    )


class Call(Base):
    """A recorded call, as ingested from object storage (PRD E8).

    `source_key` is the object key it came from and is unique: re-running the
    nightly sweep over a prefix must not create a second row for a recording
    already transcribed. That uniqueness is the idempotency guarantee, enforced
    by the database rather than by a check the task could race past.
    """

    __tablename__ = "calls"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_uri: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(String(512), unique=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int] = mapped_column(default=0)
    participants: Mapped[list[str]] = mapped_column(JSONB, default=list)
    languages: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # What the transcription of this call actually cost, from the adapter's own
    # metrics rather than a per-minute estimate.
    stt_cost_inr: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    stt_model: Mapped[str] = mapped_column(String(64), default="")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','transcribing','transcribed','failed')",
            name="ck_calls_status",
        ),
        CheckConstraint("duration_s >= 0", name="ck_calls_duration"),
    )


class TranscriptSegment(Base):
    """One diarized turn, in the language it was spoken plus a Roman rendering.

    The PRD's E8 schema has no `text_roman` column; it is added here because
    lexicon matching (P3) has to see Hinglish written either way -- the same
    phrase reaches us as Devanagari from STT and as Roman from a chat export,
    and a matcher that only sees one of them misses half the corpus. The native
    text stays authoritative: evidence spans are quoted from `text`, never from
    the transliteration.
    """

    __tablename__ = "transcript_segments"
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), primary_key=True)
    seg_id: Mapped[int] = mapped_column(primary_key=True)
    speaker: Mapped[str] = mapped_column(String(64), default="")
    start_ms: Mapped[int] = mapped_column(default=0)
    end_ms: Mapped[int] = mapped_column(default=0)
    text: Mapped[str] = mapped_column(Text)
    text_roman: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(16), default="")
    # Which transliteration produced `text_roman`: "indic-transliteration:<scheme>"
    # or "sarvam:<model>". Without it a corpus mixes two romanisations and
    # nobody can tell which row came from which.
    roman_source: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        CheckConstraint("end_ms >= start_ms", name="ck_transcript_segments_span"),
        Index("ix_transcript_segments_call", "call_id", "start_ms"),
    )


class AnalysisRun(Base):
    """One detector run over one call (PRD E8), append-only and hash-chained.

    `seq` is the ordering key for the chain, not `created_at`: a statement
    timestamp is shared by every row written in one transaction, and the
    chain's order has to be total. `created_at` is still set by the
    application rather than the server, because a `server_default` is assigned
    after the application has hashed the row -- the stored hash would then
    describe a row that does not exist.
    """

    __tablename__ = "analysis_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    lexicon_version: Mapped[str] = mapped_column(String(64), default="")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # seq is the chain order. A timestamp can tie -- two rows in one
    # transaction share a statement timestamp -- so the chain walks this.
    # `always=True`, not `always=False`: `generated by default` would let a
    # caller holding nothing but INSERT supply its own `seq`. One row with a
    # huge `seq` sorts after every future append, so every later row's
    # `prev_hash` points at the wrong predecessor -- a permanent break, and an
    # unrepairable one, because that same role has no UPDATE and no DELETE.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    row_hash: Mapped[str] = mapped_column(String(64))


class Flag(Base):
    """A candidate finding for human review (PRD E8). Append-only, hash-chained."""

    __tablename__ = "flags"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    category: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    speaker: Mapped[str] = mapped_column(String(64), default="")
    start_ms: Mapped[int] = mapped_column(default=0)
    evidence_span: Mapped[str] = mapped_column(Text)
    english_rendering: Mapped[str] = mapped_column(Text, default="")
    reasoning: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    row_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("severity in ('low','medium','high')", name="ck_flags_severity"),
    )


class Disposition(Base):
    """A reviewer's decision on a flag (PRD E8). Append-only, hash-chained.

    A changed mind is a new row, never an edit: that is what makes the chain
    worth having, and it means the queue shows the latest disposition while the
    audit shows every one.
    """

    __tablename__ = "dispositions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    flag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("flags.id"), index=True)
    disposition: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    row_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "disposition in ('confirmed','false_positive','needs_more_context','escalated')",
            name="ck_dispositions_disposition",
        ),
    )


# The out-of-chain anchor for the tables above (0009_uc3_chain_anchor).
#
# Declared here, with every other table, for one blunt reason: alembic's env.py
# builds `target_metadata` from this module's `Base` and nothing else. A table
# registered only from `comms_surveillance.audit` is a table `alembic check`
# never sees, so it finds one in the database with no model behind it and
# proposes to drop the evidence store. Core `Table` rather than a mapped class
# because the verification job reaches it through core select/insert and there
# is no object to map.
#
# Deliberately not chained: chaining the anchor would put the evidence back
# inside the thing it is evidence about.
audit_chain_anchors = Table(
    "audit_chain_anchors",
    Base.metadata,
    Column("table_name", String(64), primary_key=True),
    Column("head_hash", String(64), nullable=False),
    Column("row_count", BigInteger, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("row_count >= 0", name="ck_audit_chain_anchors_row_count"),
)
