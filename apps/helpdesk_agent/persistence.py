"""Serialize turns per session and commit the guarded decision atomically."""

import os
import uuid
from typing import Any

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
                state["clarify_count"] = sum(t.decision_json["action"] == "clarify" for t in turns)
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
