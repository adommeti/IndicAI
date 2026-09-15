"""Serialize turns per session and commit the guarded decision atomically."""

import logging
import os
import uuid
from collections.abc import Mapping
from typing import Any

from indic_platform.adapters import budget
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.embeddings import TEIEmbedder
from indic_platform.adapters.vectorstore import QdrantVectorStore
from indic_platform.config.settings import RetrievalSettings, settings
from indic_platform.db.models import Session, Turn
from indic_platform.obs.langfuse import LangfuseSink, default_sink
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from helpdesk_agent.graph import (
    MODEL,
    POLICY_VERSION,
    PROMPT_VERSION,
    Agent,
    TurnState,
    session_utterances,
)
from helpdesk_agent.grounding import TicketGrounder
from helpdesk_agent.retriever import Retriever


async def run_turn(state: TurnState, *, existing: bool = False) -> TurnState:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    embedder = TEIEmbedder(settings.tei_url, settings.sparse_url)
    client = AsyncQdrantClient(url=settings.qdrant_url)
    sink = default_sink()
    if not isinstance(sink, LangfuseSink):
        raise RuntimeError("A Langfuse sink is required for chat turns")
    try:
        # Every adapter call this turn makes -- retrieval embeddings and Claude, and on
        # the voice path Saaras and Bulbul -- is charged to this session's counter. The
        # per-session cap in `indic_platform.adapters.budget` is what stands between a
        # retry loop and an unbounded bill (PRD threat T9), and it is INERT unless
        # somebody opens the scope. This is that somebody: a turn is the unit of work
        # that owns a session id, so it is the honest place for the boundary. The scope
        # is task-local, so concurrent turns never charge each other.
        with budget.session_scope(state["session_id"]):
            async with AsyncSession(engine) as db, db.begin():
                session_id = uuid.UUID(state["session_id"])
                evidence = session_utterances(state)
                if existing:
                    session = await db.scalar(
                        select(Session).where(Session.id == session_id).with_for_update()
                    )
                    if session is None or session.app != "helpdesk_agent":
                        raise LookupError("Unknown helpdesk session")
                    if session.metadata_json.get("employee_id") != state["employee_id"]:
                        raise PermissionError("Session employee mismatch")
                    turns = list(
                        (
                            await db.scalars(
                                select(Turn)
                                .where(Turn.session_id == session_id)
                                .order_by(Turn.created_at, Turn.id)
                            )
                        ).all()
                    )
                    state["history"] = [
                        {
                            "utterance": t.utterance,
                            **{k: v for k, v in t.decision_json.items() if not k.startswith("_")},
                        }
                        for t in turns[-8:]
                    ]
                    evidence = "\n".join([t.utterance for t in turns] + [state["utterance"]])
                    state["clarify_count"] = sum(
                        t.decision_json["action"] == "clarify" for t in turns
                    )
                else:
                    db.add(
                        Session(
                            id=session_id,
                            app="helpdesk_agent",
                            metadata_json={"employee_id": state["employee_id"]},
                        )
                    )
                    await db.flush()
                # Retrieval translation stays disabled for this chat-only path.
                retriever = Retriever(
                    QdrantVectorStore(client, embedder),
                    settings=RetrievalSettings(parallel_translate=False),
                )
                metadata: dict[str, Any] = {}
                with sink.client.start_as_current_observation(
                    name="helpdesk.turn",
                    as_type="span",
                    metadata={"model": MODEL, "policy_version": POLICY_VERSION},
                ) as span:
                    trace_id = str(span.trace_id)
                    claude = Claude()
                    try:
                        result = await Agent(
                            retrieve=retriever.retrieve,
                            structured=claude.structured,
                            grounder=TicketGrounder(structured=claude.structured),
                            # The act node files the ticket, so it needs the session
                            # this turn is already running in -- it does not commit;
                            # this transaction owns that.
                            db=db,
                            # The real count of turns already persisted, NOT
                            # `len(state["history"])`. History is truncated to the
                            # last 8 turns both here and in `initial_state`, so
                            # turns 9 and 10 would both present a `turn_index` of 8
                            # and the second ticket would be swallowed as an
                            # idempotent replay of the first. Idempotency keyed on a
                            # value that repeats is silent data loss, and the
                            # employee would be told their ticket was filed.
                            turn_index=len(turns) if existing else 0,
                        ).run(state, metadata=metadata, evidence=evidence)
                    finally:
                        await claude.client.close()
                    decision = result["decision"]
                    assert decision is not None
                    # Keep persisted content locally; vendor/log exports are redacted by adapters.
                    db.add(
                        Turn(
                            session_id=session_id,
                            utterance=state["utterance"],
                            language=state["language"],
                            decision_json={
                                **decision.model_dump(mode="json"),
                                "_grounding": metadata["ticket_grounding"],
                                "_guard_errors": metadata["guard_errors"],
                            },
                            retrieval_json=metadata["retrieval_json"],
                            latency_ms=metadata["latency_ms"],
                            policy_version=POLICY_VERSION,
                            prompt_version=PROMPT_VERSION,
                            model=MODEL,
                            trace_id=trace_id,
                        )
                    )
                    span.update(
                        metadata={
                            "model": MODEL,
                            "policy_version": POLICY_VERSION,
                            "action": decision.action,
                            "fallback": metadata.get("fallback", False),
                            "guard_errors": metadata["guard_errors"],
                            "grounding_checks": len(metadata["ticket_grounding"]["checks"]),
                            "latency_ms": metadata["latency_ms"],
                        }
                    )
        return result
    finally:
        await embedder.close()
        await client.close()
        await engine.dispose()
        sink.flush()


def merge_latency(current: Mapping[str, Any] | None, voice: Mapping[str, float]) -> dict[str, Any]:
    """Merge measured voice stages onto a turn's existing `latency_ms` payload.

    Pure, so the arithmetic is provable without a database. The graph's own node timings
    (`retrieve`, `decide`, `guard`, `act`, `total`) are already in `current` and survive:
    the voice stages arrive namespaced under `voice.` by
    `voice_pipeline.StageLatency.merge_into`, which is why nothing here re-prefixes them.
    A stage that did not run is absent from `voice` and stays absent -- never 0.0.
    """
    return {**(current or {}), **{name: float(value) for name, value in voice.items()}}


async def record_voice_latency(
    session_id: str, turn_index: int, voice: Mapping[str, float]
) -> bool:
    """Merge one voice turn's stage latencies into its already-committed `turns` row.

    This is an UPDATE, in its own transaction, because of when the numbers exist.
    `run_turn` commits the turn while the graph's `decide` is still on the stack, but
    `tts_ms` and `time_to_first_audio_ms` are only known once first audio has actually
    been emitted -- strictly later than that commit. There is no moment at which a single
    INSERT could carry both halves, so the voice half lands afterwards.

    `turn_index` addresses the ``turn_index``-th turn of the session under
    ``order_by(Turn.created_at, Turn.id)`` -- literally the ordering `run_turn` uses to
    rebuild a session's history. The two must match: under any other ordering, index *n*
    here and index *n* there are different rows, and a latency would be filed against
    someone else's turn. `created_at` defaults to Postgres ``now()``, the *transaction*
    timestamp, and every turn commits in its own transaction, so the order is the order
    the turns were taken; `Turn.id` only breaks a tie that real turns cannot produce.

    Returns True when a row was updated, False when the session has no such turn. Absent
    is a legitimate answer, not an error: the eval path runs the graph without a voice
    session, so a caller can hold an index that was never persisted.

    A latency write must never fail the turn it describes -- the employee has already
    been answered, and losing a metric is not worth losing a reply. Every failure is
    swallowed and logged by exception class only: no utterance, no reply, no identifiers.
    """
    log = logging.getLogger(__name__)
    if turn_index < 0:
        return False
    try:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine) as db, db.begin():
                turn = await db.scalar(
                    select(Turn)
                    .where(Turn.session_id == uuid.UUID(session_id))
                    .order_by(Turn.created_at, Turn.id)
                    .offset(turn_index)
                    .limit(1)
                    .with_for_update()
                )
                if turn is None:
                    log.info("no turn %d for this voice session; latency not recorded", turn_index)
                    return False
                turn.latency_ms = merge_latency(turn.latency_ms, voice)
            return True
        finally:
            await engine.dispose()
    except Exception as error:
        log.warning("voice latency not recorded (%s)", type(error).__name__)
        return False
