"""Append-only, hash-chained audit trail (PRD E8).

    row_hash = sha256(prev_hash || canonical_json(row_without_hashes))

The point of the chain is not secrecy -- anyone with SELECT can read every row.
It is that a row cannot be *changed* without every later row's hash becoming
wrong. Combined with a DB role that has no UPDATE or DELETE, that makes silent
tampering require both a privilege escalation and a rewrite of the whole tail,
and the nightly `chain_verify` turns the second half into an alert.

Three details the property depends on, each easy to get subtly wrong:

**Canonical JSON.** Sorted keys, no whitespace, UTF-8, and `default=str` for the
types JSON has no opinion about (UUID, datetime, Decimal). Two processes hashing
the same row must produce the same bytes or the chain breaks for no reason.

**A total order.** `created_at` alone is not one: two rows written in the same
transaction share a statement timestamp. Each table carries a `seq` bigserial
and the chain walks that.

**Serialised appends.** Computing `prev_hash` is a read-modify-write, so two
concurrent inserts can both read the same head and produce a fork -- two rows
with the same `prev_hash`, one of which every later verification will call a
break. `append` takes a transaction-scoped advisory lock per table, so the
window closes at the database rather than by hoping.
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from indic_platform.db.models import AnalysisRun, Disposition, Flag
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

# The columns that are not part of what is hashed: the hashes themselves, and
# `seq`, which the database assigns after the application has hashed the row.
NOT_HASHED = frozenset({"prev_hash", "row_hash", "seq"})

CHAINED = {"analysis_runs": AnalysisRun, "flags": Flag, "dispositions": Disposition}

# Distinct per table, so an append to `flags` does not block one to
# `dispositions`. Arbitrary but fixed: changing them changes nothing about
# stored data, only about which appends serialise against each other.
LOCK_KEYS = {"analysis_runs": 0x7C3A_0001, "flags": 0x7C3A_0002, "dispositions": 0x7C3A_0003}

GENESIS = ""

log = logging.getLogger(__name__)


def canonical_json(payload: dict[str, Any]) -> bytes:
    """The exact bytes that get hashed.

    `sort_keys` so key order cannot change the hash, `separators` so whitespace
    cannot, `ensure_ascii=False` so a Devanagari evidence span hashes as itself
    rather than as escape sequences, and `default=str` so UUIDs and datetimes
    have one representation instead of raising.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def row_payload(row: Any) -> dict[str, Any]:
    """Everything about a row except the hashes and the sequence number."""
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns
        if column.name not in NOT_HASHED
    }


def row_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """`sha256(prev_hash || canonical_json(row_without_hashes))`, E8 verbatim."""
    return hashlib.sha256(prev_hash.encode("utf-8") + canonical_json(payload)).hexdigest()


async def head(session: AsyncSession, table: str) -> str:
    """The last row's hash, or the genesis value for an empty chain."""
    model = CHAINED[table]
    result = await session.execute(
        select(model.row_hash).order_by(model.seq.desc()).limit(1)  # type: ignore[attr-defined]
    )
    return result.scalar_one_or_none() or GENESIS


async def append(session: AsyncSession, row: Any) -> Any:
    """Add one row to its chain. The only supported way to write these tables.

    The advisory lock is transaction-scoped, so it is released by the commit or
    the rollback and never leaks. It is what makes "read the head, hash against
    it, insert" atomic with respect to another appender.
    """
    table = row.__table__.name
    if table not in CHAINED:
        raise ValueError(f"{table} is not a chained table")
    await session.execute(text("select pg_advisory_xact_lock(:key)"), {"key": LOCK_KEYS[table]})

    materialise_defaults(row)
    previous = await head(session, table)
    row.prev_hash = previous
    row.row_hash = row_hash(previous, row_payload(row))
    session.add(row)
    await session.flush()
    return row


def materialise_defaults(row: Any) -> None:
    """Apply the column defaults *before* hashing, not during the flush.

    SQLAlchemy fills Python-side defaults (`default=uuid.uuid4`, `default=""`)
    when it emits the INSERT -- which is after `append` has computed the hash.
    The row that got hashed therefore had `id=None` and the row that got stored
    had a UUID, so every chain broke at its first row with "row_hash does not
    match the row content". It looked exactly like tampering, which is the worst
    way for this bug to present: the control crying wolf teaches people to
    ignore it.

    Assigning them here means the hash covers the row that is actually written.
    """
    if getattr(row, "created_at", None) is None:
        row.created_at = datetime.now(UTC)
    for column in row.__table__.columns:
        if column.name in NOT_HASHED or getattr(row, column.name, None) is not None:
            continue
        default = column.default
        if default is None:
            continue
        value = default.arg
        setattr(row, column.name, value(None) if callable(value) else value)


@dataclass
class ChainResult:
    """What a walk of one chain found."""

    table: str
    rows: int
    ok: bool
    first_break_seq: int | None = None
    first_break_id: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows": self.rows,
            "ok": self.ok,
            "first_break_seq": self.first_break_seq,
            "first_break_id": self.first_break_id,
            "reason": self.reason,
        }


async def verify_chain(session: AsyncSession, table: str, *, batch: int = 2000) -> ChainResult:
    """Re-walk one chain and report the first row that does not add up.

    Two ways a row fails, and they mean different things:

    - **`prev_hash` does not match the previous row's `row_hash`** -- a row was
      inserted, deleted or reordered.
    - **`row_hash` does not match the row's own content** -- a row was edited.

    Streamed in batches because this runs nightly over a table that only grows,
    and loading it whole would eventually be the reason the job fails.
    """
    model = CHAINED[table]
    expected = GENESIS
    rows = 0
    offset = 0
    while True:
        chunk = (
            (
                await session.execute(
                    select(model).order_by(model.seq).offset(offset).limit(batch)  # type: ignore[attr-defined]
                )
            )
            .scalars()
            .all()
        )
        if not chunk:
            break
        for row in chunk:
            rows += 1
            if row.prev_hash != expected:
                return ChainResult(
                    table=table,
                    rows=rows,
                    ok=False,
                    first_break_seq=row.seq,
                    first_break_id=str(row.id),
                    reason=(
                        "prev_hash does not match the previous row: inserted, deleted or reordered"
                    ),
                )
            recomputed = row_hash(row.prev_hash, row_payload(row))
            if recomputed != row.row_hash:
                return ChainResult(
                    table=table,
                    rows=rows,
                    ok=False,
                    first_break_seq=row.seq,
                    first_break_id=str(row.id),
                    reason="row_hash does not match the row content: the row was edited",
                )
            expected = row.row_hash
        offset += batch
    return ChainResult(table=table, rows=rows, ok=True)


async def verify_all(session: AsyncSession) -> list[ChainResult]:
    return [await verify_chain(session, table) for table in CHAINED]


async def counts(session: AsyncSession) -> dict[str, int]:
    out = {}
    for table, model in CHAINED.items():
        out[table] = int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )
    return out


def summarise(results: Sequence[ChainResult]) -> dict[str, Any]:
    """The shape the beat job logs and the metric is derived from."""
    broken = [r for r in results if not r.ok]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "tables": [r.as_dict() for r in results],
        "rows": sum(r.rows for r in results),
        "breaks": len(broken),
        "ok": not broken,
    }


# --- the nightly job ---------------------------------------------------------


# The metric a monitor alerts on. A gauge rather than a counter: "how many
# chains are currently broken" is the question an on-call person has, and a
# counter would keep the alert firing after the breach was understood.
CHAIN_BREAKS_METRIC = "uc3_audit_chain_breaks"


async def run_chain_verify(session_factory: Any) -> dict[str, Any]:
    """Re-walk every chain and emit the alert metric. Returns the summary."""
    async with session_factory() as session:
        summary = summarise(await verify_all(session))

    emit_chain_metric(summary)
    if not summary["ok"]:
        # Loud, because a break means either a bug in the append path or
        # somebody with more privilege than the app editing the audit trail,
        # and both need a person tonight.
        log.error("uc3 audit chain broken: %s", summary)
    else:
        log.info("uc3 audit chain verified: %(rows)s rows, no breaks", summary)
    return summary


def emit_chain_metric(summary: dict[str, Any]) -> None:
    """Publish the break count where the monitoring stack can see it.

    Metadata only, never row content: a Langfuse span or a Prometheus sample
    carrying an evidence span would put the transcript somewhere the audit
    tables' access controls do not reach.
    """
    from indic_platform.obs.langfuse import default_sink

    default_sink().emit(
        {
            "event": CHAIN_BREAKS_METRIC,
            "value": summary["breaks"],
            "rows": summary["rows"],
            "tables": {t["table"]: t["ok"] for t in summary["tables"]},
        }
    )
