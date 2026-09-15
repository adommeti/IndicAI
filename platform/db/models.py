import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Numeric, String, Text, func, text
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
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
