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
    UniqueConstraint,
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
    # A caller-supplied de-duplication token (0013_uc3_disposition_idempotency).
    # Nullable because it is optional and because the column was added to a table
    # that already had rows; PostgreSQL treats NULLs as distinct, so unkeyed writes
    # stay unconstrained while two writes sharing a key for one flag collide at the
    # database. Deliberately absent from `audit.HASHED_COLUMNS`: the chain covers the
    # decision, and this is a fact about the delivery of the request, not about it.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    row_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "disposition in ('confirmed','false_positive','needs_more_context','escalated')",
            name="ck_dispositions_disposition",
        ),
        UniqueConstraint(
            "flag_id",
            "reviewer_id",
            "idempotency_key",
            name="uq_dispositions_flag_idempotency_key",
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


# --- UC1 helpdesk ticketing (uc1/P4) -----------------------------------------


class TicketFiling(Base):
    """One turn's attempt to file a helpdesk ticket in Zammad.

    A ticket is a side effect the employee can see, so filing it twice is worse
    than not filing it at all. The guarantee is `uq_ticket_filings_turn`: one row
    per `(session_id, turn_index)`, refused by the database. A retry of the same
    turn -- a reconnecting voice client, a Celery redelivery, two workers racing
    -- reserves the same key, loses on the constraint, and reads back what the
    winner filed. A read-then-write check ("is there a row? no -- create one")
    cannot do this: both readers see nothing and both file.

    `ck_ticket_filings_number` is the other half. A row may carry a ticket number
    only when it is `filed`; a `pending` row that somehow acquired a number is
    refused at INSERT rather than read back to an employee as if Zammad had
    issued it. The number is Zammad's to mint and nobody else's.

    `payload` is the ticket as the graph produced it, stored so the fallback task
    can file exactly what was approved without re-running the model, and
    `source_key` is the value written to Zammad's `source_session_id` custom
    field -- the handle a retry searches on when it cannot tell whether the
    previous attempt committed at Zammad before the connection dropped.
    """

    __tablename__ = "ticket_filings"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    turn_index: Mapped[int]
    employee_id: Mapped[str] = mapped_column(String(128))
    source_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="filing")
    ticket_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ticket_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    # A short class name, never a vendor message: this column is read in logs.
    last_error: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "turn_index", name="uq_ticket_filings_turn"),
        CheckConstraint("status in ('filing','filed','pending')", name="ck_ticket_filings_status"),
        CheckConstraint("turn_index >= 0", name="ck_ticket_filings_turn_index"),
        CheckConstraint(
            "(status = 'filed') = (ticket_number is not null)",
            name="ck_ticket_filings_number",
        ),
    )


# --- UC1 retention (PRD C8; uc1/P7) ------------------------------------------


class RetentionDeletion(Base):
    """One retention policy's pass over one store: what it removed, and by what rule.

    This table exists to answer a question asked a year later, by someone who
    cannot see the code that ran: *was this data deleted on time, and under
    which rule?* Answering it needs four things a bare "deleted N rows" line
    does not carry.

    The rule: `policy`, `retention_days` and `cutoff_at`. A retention window is
    configurable, so a row deleted under a 90-day rule and a row deleted after
    the window was shortened to 30 are different events; storing the number in
    force at the time -- and the cutoff it produced -- makes the pass
    reconstructible without the deployment's environment. `cutoff_at` is the
    answer to "on time": everything created before it was in scope, and
    `started_at - retention_days = cutoff_at` is the invariant a reviewer checks.

    The scope: `target` (the store: the table name for a Postgres policy,
    `turns`; a bucket/prefix for an object-store policy) and the
    `window_start`/`window_end` pair, which is the oldest and newest
    `created_at` the pass actually covered. A count alone cannot show that the
    deleted rows were the old ones; the window can, and it is NULL exactly when
    nothing matched.

    The result: `rows_matched` against `rows_deleted`. They differ for the one
    reason the flag records -- `dry_run`, where the job reports what it would
    remove and removes nothing (`ck_retention_deletions_dry_run` refuses any
    other reading of a dry run). `batches` and `batch_size` show the work was
    chunked, and make a `failed` row legible: a pass that died after four
    batches deleted four batches' worth, and the rows it did delete are still
    counted here.

    The provenance: `job_run_id` groups the policies of one sweep -- audio and
    transcripts fire together -- so a reviewer sees a whole night's pass rather
    than two unrelated rows, and `app` keeps one shared table honest when a
    second application starts deleting its own data through it.

    Nothing here is a copy of what was deleted: no ids, no utterances, no keys.
    An audit record of a privacy deletion that quotes the deleted text would
    defeat the deletion.
    """

    __tablename__ = "retention_deletions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_run_id: Mapped[uuid.UUID]
    app: Mapped[str] = mapped_column(String(64))
    policy: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200))
    retention_days: Mapped[int]
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_matched: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_deleted: Mapped[int] = mapped_column(BigInteger, default=0)
    batches: Mapped[int] = mapped_column(default=0)
    batch_size: Mapped[int] = mapped_column(default=0)
    dry_run: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="completed")
    # Why a pass matched nothing, or the class name of what stopped it. Never
    # the deleted content.
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status in ('completed','failed')", name="ck_retention_deletions_status"),
        CheckConstraint("retention_days > 0", name="ck_retention_deletions_days"),
        CheckConstraint("batch_size > 0 and batches >= 0", name="ck_retention_deletions_batching"),
        CheckConstraint(
            "rows_matched >= 0 and rows_deleted >= 0 and rows_deleted <= rows_matched",
            name="ck_retention_deletions_counts",
        ),
        # A dry run that deleted something is not a dry run.
        CheckConstraint("not dry_run or rows_deleted = 0", name="ck_retention_deletions_dry_run"),
        # The window is present exactly when the pass covered something, and
        # never runs backwards.
        CheckConstraint(
            "(window_start is null) = (window_end is null)",
            name="ck_retention_deletions_window_pair",
        ),
        CheckConstraint(
            "(rows_matched = 0) = (window_start is null)",
            name="ck_retention_deletions_window_scope",
        ),
        CheckConstraint(
            "window_end is null or window_end >= window_start",
            name="ck_retention_deletions_window_order",
        ),
        CheckConstraint("finished_at >= started_at", name="ck_retention_deletions_duration"),
        Index("ix_retention_deletions_policy_finished", "policy", "finished_at"),
        Index("ix_retention_deletions_job_run", "job_run_id"),
    )
