"""Append-only, hash-chained audit trail (PRD E8).

    row_hash = sha256(prev_hash || canonical_json(row_without_hashes))

The point of the chain is not secrecy -- anyone with SELECT can read every row.
It is that a row cannot be *changed* without every later row's hash becoming
wrong. Combined with a DB role that has no UPDATE or DELETE, that makes silent
tampering require both a privilege escalation and a rewrite of the whole tail,
and the nightly `chain_verify` turns the second half into an alert.

## What this actually defeats, and what it does not

Be precise about the claim, because "hash chain" invites a stronger reading
than the construction supports.

It **does** detect, against an attacker who does not hold `uc3_app`'s superuser:
an edited row, an inserted row, a deleted row, a reordered chain -- the walk
finds the first row whose hashes stop adding up. Adding the per-table anchor
below, it also detects a *truncated tail* and an *emptied table*, which the
walk alone cannot: the surviving prefix is internally consistent and verifies
clean, so `delete from analysis_runs where seq > 400` used to pass silently.

It **does not** defeat a tamperer who has superuser AND write access to the
anchor row. The digest is an unkeyed sha256, so anyone who can write the tables
can also recompute every hash; the acceptance benchmark walks 10k rows in
0.46s, so re-chaining an edited history is seconds of work, not a deterrent.
Such a tamperer edits the row, re-chains the tail, overwrites the anchor with
the new head and row count, and every later verification reports clean. The
anchor raises the bar -- two writes in two places instead of one -- but it is
not a cryptographic barrier, because it sits in the same database under the
same superuser.

Genuine tamper-*evidence* needs an anchor the tamperer cannot recompute: the
daily head hash signed with a key the database host does not hold, notarised to
an append-only external log, or written to a WORM object with a retention lock.
That is deployment scope, not code, and until it exists this control should be
described as "detects tampering by anything short of database superuser", never
as "the audit trail is immutable".

## Three details the property depends on, each easy to get subtly wrong

**Canonical JSON.** Sorted keys, no whitespace, UTF-8, and one fixed rendering
for the types JSON has no opinion about (UUID, datetime, Decimal). Two
processes hashing the same row must produce the same bytes or the chain breaks
for no reason. Datetimes are normalised to UTC before they are rendered:
psycopg3 hands back a `timestamptz` in the *session's* TimeZone, so the same
instant arrives as `+05:30` on one connection and `+00:00` on another, and
hashing the offset would make a server or connection with a non-UTC TimeZone
fail verification on every row forever.

**A pinned column list.** What is hashed per table is `HASHED_COLUMNS`, written
out explicitly, not whatever `__table__.columns` happens to hold at verify time.
Deriving it meant any future `ALTER TABLE ... ADD COLUMN` retroactively changed
the payload of every historical row and broke all three chains at row 1 --
mass tampering alerts caused by a migration. With the list pinned, adding a
column is inert; changing what the hash covers means bumping
`HASH_SCHEMA_VERSION`, which invalidates stored hashes deliberately and loudly
rather than silently.

**A total order.** `created_at` alone is not one: two rows written in the same
transaction share a statement timestamp. Each table carries a `seq` bigserial
and the chain walks that, by keyset (`seq > last`) rather than OFFSET, because
OFFSET paging is O(n^2) over a table designed only to grow.

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

from indic_platform.db.models import AnalysisRun, Base, Disposition, Flag
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    String,
    Table,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# The columns that are not part of what is hashed: the hashes themselves, `seq`,
# which the database assigns after the application has hashed the row, and
# `idempotency_key`, which describes the delivery of a request rather than the
# decision it carried (0013_uc3_disposition_idempotency). The chain covers the flag,
# the verdict, the note and the identity that signed it; a retry token is not part of
# a reviewer's ruling, and hashing it would have meant bumping HASH_SCHEMA_VERSION and
# invalidating every stored hash to add a de-duplication field.
NOT_HASHED = frozenset({"prev_hash", "row_hash", "seq", "idempotency_key"})

CHAINED = {"analysis_runs": AnalysisRun, "flags": Flag, "dispositions": Disposition}

# Which columns each row's hash covers, in a fixed order, pinned rather than
# read from the mapper. See the module docstring: derived column lists turn an
# unrelated ADD COLUMN into a chain-wide false alarm.
HASHED_COLUMNS: dict[str, tuple[str, ...]] = {
    "analysis_runs": (
        "id",
        "call_id",
        "stage",
        "model",
        "policy_version",
        "lexicon_version",
        "prompt_version",
        "input_sha256",
        "output",
        "created_at",
    ),
    "flags": (
        "id",
        "call_id",
        "run_id",
        "category",
        "severity",
        "speaker",
        "start_ms",
        "evidence_span",
        "english_rendering",
        "reasoning",
        "created_at",
    ),
    "dispositions": (
        "id",
        "flag_id",
        "disposition",
        "note",
        "reviewer_id",
        "created_at",
    ),
}

# Which *set* of columns and which rendering the hash covers. It is part of the
# hashed payload so that a row carries the version it was written under. Bumping
# it changes every hash, so it is a re-anchoring event that needs an ADR and a
# documented cutover -- which is the point: a deliberate, visible break instead
# of a silent one.
HASH_SCHEMA_VERSION = 1

# Distinct per table, so an append to `flags` does not block one to
# `dispositions`. Arbitrary but fixed: changing them changes nothing about
# stored data, only about which appends serialise against each other.
LOCK_KEYS = {"analysis_runs": 0x7C3A_0001, "flags": 0x7C3A_0002, "dispositions": 0x7C3A_0003}

GENESIS = ""

log = logging.getLogger(__name__)


def _validate_hashed_columns() -> None:
    """Fail at import if a pinned column is not a real, hashable column.

    A rename or a drop silently turns every row's payload into one missing a
    field, which presents as universal tampering. Catching it here makes it a
    startup error in the deploy that introduced it instead of an alert at 02:00.
    """
    for table, columns in HASHED_COLUMNS.items():
        actual = {c.name for c in CHAINED[table].__table__.columns}
        missing = [c for c in columns if c not in actual]
        if missing:
            raise RuntimeError(f"{table}: hashed columns {missing} do not exist on the model")
        overlap = [c for c in columns if c in NOT_HASHED]
        if overlap:
            raise RuntimeError(
                f"{table}: {overlap} cannot be hashed; a row cannot contain its hash"
            )


_validate_hashed_columns()


def _json_default(value: Any) -> str:
    """One fixed rendering for the types JSON has no opinion about.

    Datetimes are pinned to UTC first. A tz-aware value rendered with its own
    offset hashes differently depending on the *reader's* session TimeZone,
    which psycopg3 attaches to every `timestamptz` it returns -- so a replica or
    a connection with `TimeZone=Asia/Kolkata` would report the entire history as
    tampered. Naive values are read as UTC rather than local: everything written
    here is `datetime.now(UTC)`, and guessing the host's zone would be the one
    way to turn a portable hash back into a machine-dependent one.
    """
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    return str(value)


def canonical_json(payload: dict[str, Any]) -> bytes:
    """The exact bytes that get hashed.

    `sort_keys` so key order cannot change the hash, `separators` so whitespace
    cannot, `ensure_ascii=False` so a Devanagari evidence span hashes as itself
    rather than as escape sequences, and `_json_default` so UUIDs, datetimes and
    Decimals have one representation instead of raising.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def row_payload(row: Any) -> dict[str, Any]:
    """The pinned columns of a row, plus the version of the pinning itself."""
    table = row.__table__.name
    if table not in HASHED_COLUMNS:
        raise ValueError(f"{table} has no pinned hash schema")
    payload: dict[str, Any] = {name: getattr(row, name) for name in HASHED_COLUMNS[table]}
    # Reserved key: no chained table has a column starting with an underscore,
    # so this cannot collide with row content.
    payload["_hash_schema"] = HASH_SCHEMA_VERSION
    return payload


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


# --- the anchor --------------------------------------------------------------


ANCHOR_TABLE_NAME = "audit_chain_anchors"


def _anchor_table() -> Table:
    """The anchor table, registered on the shared metadata exactly once.

    Defined here rather than in `models.py` because it is not a chained table
    and nothing else touches it -- but it must still be in `Base.metadata`, or
    `alembic check` sees a table in the database with no model and proposes to
    drop it. The lookup guard keeps this safe if the table is later promoted to
    a mapped class: whoever defines it first wins, and this does not redefine it.
    """
    existing = Base.metadata.tables.get(ANCHOR_TABLE_NAME)
    if existing is not None:
        return existing
    return Table(
        ANCHOR_TABLE_NAME,
        Base.metadata,
        Column("table_name", String(64), primary_key=True),
        Column("head_hash", String(64), nullable=False),
        Column("row_count", BigInteger, nullable=False),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
    )


ANCHORS = _anchor_table()


@dataclass(frozen=True)
class ChainAnchor:
    """What one chain looked like the last time it verified clean.

    Kept outside the chained tables on purpose. Everything inside a chain is
    self-consistent after a tail truncation -- the remaining prefix still hashes
    correctly -- so the only evidence that rows are *missing from the end* has
    to live somewhere the truncation did not reach.
    """

    table: str
    head_hash: str
    rows: int
    recorded_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "head_hash": self.head_hash,
            "rows": self.rows,
            "recorded_at": self.recorded_at.isoformat(),
        }


async def read_anchor(session: AsyncSession, table: str) -> ChainAnchor | None:
    """The stored anchor for one chain, or None if none has been recorded yet."""
    row = (
        await session.execute(
            select(
                ANCHORS.c.table_name,
                ANCHORS.c.head_hash,
                ANCHORS.c.row_count,
                ANCHORS.c.recorded_at,
            ).where(ANCHORS.c.table_name == table)
        )
    ).first()
    if row is None:
        return None
    recorded_at = row.recorded_at
    return ChainAnchor(
        table=row.table_name,
        head_hash=row.head_hash,
        rows=int(row.row_count),
        recorded_at=recorded_at if recorded_at.tzinfo else recorded_at.replace(tzinfo=UTC),
    )


async def write_anchor(
    session: AsyncSession, table: str, *, head_hash: str, rows: int
) -> ChainAnchor:
    """Record where a chain ended. Only ever called after a clean verification.

    Upsert rather than insert: there is one anchor per chain and it moves
    forward, so the history that matters is the chain itself. Anchoring a chain
    that did *not* verify would launder whatever broke it into the new baseline.
    """
    recorded_at = datetime.now(UTC)
    statement = pg_insert(ANCHORS).values(
        table_name=table, head_hash=head_hash, row_count=rows, recorded_at=recorded_at
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[ANCHORS.c.table_name],
            set_={
                "head_hash": statement.excluded.head_hash,
                "row_count": statement.excluded.row_count,
                "recorded_at": statement.excluded.recorded_at,
            },
        )
    )
    return ChainAnchor(table=table, head_hash=head_hash, rows=rows, recorded_at=recorded_at)


# --- verification ------------------------------------------------------------


@dataclass
class ChainResult:
    """What a walk of one chain found."""

    table: str
    rows: int
    ok: bool
    first_break_seq: int | None = None
    first_break_id: str | None = None
    reason: str = ""
    # The chain's current head, so the caller can move the anchor without a
    # second query.
    head_hash: str = GENESIS
    # What the anchor said, and whether the chain still agrees with it. None
    # means no anchor was compared -- an un-anchored chain, or `check_anchor`
    # off -- which is a weaker result than True and should not be read as one.
    anchor_rows: int | None = None
    anchor_ok: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "rows": self.rows,
            "ok": self.ok,
            "first_break_seq": self.first_break_seq,
            "first_break_id": self.first_break_id,
            "reason": self.reason,
            "head_hash": self.head_hash,
            "anchor_rows": self.anchor_rows,
            "anchor_ok": self.anchor_ok,
        }


async def verify_chain(
    session: AsyncSession, table: str, *, batch: int = 2000, check_anchor: bool = True
) -> ChainResult:
    """Re-walk one chain and report the first row that does not add up.

    Three ways a chain fails, and they mean different things:

    - **`prev_hash` does not match the previous row's `row_hash`** -- a row was
      inserted, deleted or reordered.
    - **`row_hash` does not match the row's own content** -- a row was edited.
    - **the chain no longer reaches the anchor** -- it is shorter than when it
      last verified, or the head it ended on is gone. Neither shows up in the
      walk, because a truncated chain is a valid chain; see `ChainAnchor`.

    Streamed in batches because this runs nightly over a table that only grows,
    and loading it whole would eventually be the reason the job fails. Paged by
    `seq > last_seq` rather than OFFSET so the cost stays linear in the table.
    """
    model = CHAINED[table]
    expected = GENESIS
    rows = 0
    # None rather than 0, so the first page has no lower bound at all: a
    # numeric sentinel silently skips any row sitting at or below it, and a row
    # the walk never reads is the one a tamperer would most like to plant.
    last_seq: int | None = None
    anchor = await read_anchor(session, table) if check_anchor else None
    # An anchor taken when the chain was empty points at genesis, which every
    # chain trivially contains.
    anchor_head_seen = anchor is None or anchor.head_hash == GENESIS
    anchor_rows = anchor.rows if anchor is not None else None

    while True:
        page = select(model).order_by(model.seq).limit(batch)  # type: ignore[attr-defined]
        if last_seq is not None:
            page = page.where(model.seq > last_seq)  # type: ignore[attr-defined]
        chunk = (await session.execute(page)).scalars().all()
        if not chunk:
            break
        for row in chunk:
            rows += 1
            last_seq = row.seq
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
                    head_hash=expected,
                    anchor_rows=anchor_rows,
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
                    head_hash=expected,
                    anchor_rows=anchor_rows,
                )
            expected = row.row_hash
            if anchor is not None and row.row_hash == anchor.head_hash:
                anchor_head_seen = True

    if anchor is not None:
        # No `first_break_seq`: the evidence is the rows that are *not* there,
        # so there is no row to point at.
        if rows < anchor.rows:
            return ChainResult(
                table=table,
                rows=rows,
                ok=False,
                reason=(
                    f"the chain has {rows} rows but verified clean at {anchor.rows} on "
                    f"{anchor.recorded_at.isoformat()}: rows were removed from the end"
                ),
                head_hash=expected,
                anchor_rows=anchor.rows,
                anchor_ok=False,
            )
        if not anchor_head_seen:
            return ChainResult(
                table=table,
                rows=rows,
                ok=False,
                reason=(
                    "the head this chain last verified at is no longer in it: the tail was "
                    "rewritten, not extended"
                ),
                head_hash=expected,
                anchor_rows=anchor.rows,
                anchor_ok=False,
            )

    return ChainResult(
        table=table,
        rows=rows,
        ok=True,
        head_hash=expected,
        anchor_rows=anchor_rows,
        anchor_ok=None if anchor is None else True,
    )


async def verify_all(session: AsyncSession, *, check_anchor: bool = True) -> list[ChainResult]:
    return [await verify_chain(session, table, check_anchor=check_anchor) for table in CHAINED]


async def counts(session: AsyncSession) -> dict[str, int]:
    out = {}
    for table, model in CHAINED.items():
        out[table] = int(
            (await session.execute(select(func.count()).select_from(model))).scalar_one()
        )
    return out


def break_detail(summary: dict[str, Any]) -> str:
    """Which chains broke, how big they are and why -- and nothing else.

    The alert has to stay loud: "a person needs to look tonight" is the whole
    reason the line exists, so the table names, the row counts and the reason
    stay in it.

    What comes out is `head_hash` and `first_break_id`. Those are precisely the
    two fields `GET /audit/chain_status` refuses to project, and for reasons
    that do not stop at the HTTP boundary: a head hash is the value the anchor
    is compared against, so publishing it hands a would-be tamperer the target
    to re-chain to, and `first_break_id` names a row in the evidence store.
    Application logs are read by more people than that endpoint is, and they are
    shipped off-box; withholding a field from the API and then writing it to the
    log is the same exposure through a wider channel.

    Dropping them is not the same as losing them. The reason string says which
    of the three failures it was, and anyone with database access re-runs
    `verify_chain` to get the seq and the id -- which is the access they need to
    act on the break anyway.
    """
    broken = [table for table in summary["tables"] if not table["ok"]]
    if not broken:  # `summarise` disagreeing with itself; say so rather than "".
        return "no table reported a break"
    return "; ".join(
        f"{table['table']} ({table['rows']} rows: {table['reason'] or 'no reason recorded'})"
        for table in broken
    )


def summarise(results: Sequence[ChainResult]) -> dict[str, Any]:
    """The shape the beat job logs and the metric is derived from."""
    broken = [r for r in results if not r.ok]
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "tables": [r.as_dict() for r in results],
        "rows": sum(r.rows for r in results),
        "breaks": len(broken),
        "ok": not broken,
        # Chains with no anchor to compare against. They verified internally,
        # which does not rule out a truncation, so a first run says so instead
        # of implying a guarantee it cannot make yet.
        "unanchored": [r.table for r in results if r.anchor_ok is None],
    }


# --- the nightly job ---------------------------------------------------------


# The metric a monitor alerts on. A gauge rather than a counter: "how many
# chains are currently broken" is the question an on-call person has, and a
# counter would keep the alert firing after the breach was understood.
CHAIN_BREAKS_METRIC = "uc3_audit_chain_breaks"

# The sink's record shape is built for vendor calls. This is an internal
# operational metric, so it declares itself as one and books no model, no units
# and no spend rather than borrowing a vendor's name to fit the schema.
METRIC_VENDOR = "indic_platform"
METRIC_CAPABILITY = "uc3_audit_chain_verify"
METRIC_MODEL = "none"


async def run_chain_verify(session_factory: Any, *, update_anchors: bool = True) -> dict[str, Any]:
    """Re-walk every chain, alert, and re-anchor if clean. Returns the summary.

    Order matters and is the reason this function exists at all: the log comes
    first, then the metric, then any write. A break must be on disk in the
    application log before anything that can fail gets a turn -- the earlier
    version emitted the metric first with a record the sink rejected, so the
    KeyError pre-empted `log.error` and the alert path had never once run.
    """
    async with session_factory() as session:
        results = await verify_all(session)
    summary = summarise(results)
    summary["anchors_updated"] = []

    if not summary["ok"]:
        # Loud, because a break means either a bug in the append path or
        # somebody with more privilege than the app editing the audit trail,
        # and both need a person tonight. Loud, but not a disclosure: see
        # `break_detail`.
        log.error(
            "uc3 audit chain broken: %s of %s chains failed verification, %s rows checked: %s. "
            "Row identifiers are withheld here -- run "
            "`comms_surveillance.audit.verify_chain(session, <table>)` against the database "
            "for the breaking seq and row id.",
            summary["breaks"],
            len(summary["tables"]),
            summary["rows"],
            break_detail(summary),
        )
    else:
        log.info("uc3 audit chain verified: %s rows, no breaks", summary["rows"])

    summary["metric_emitted"] = emit_chain_metric(summary)

    if summary["ok"] and update_anchors:
        async with session_factory() as session:
            for result in results:
                await write_anchor(
                    session, result.table, head_hash=result.head_hash, rows=result.rows
                )
            await session.commit()
        summary["anchors_updated"] = [r.table for r in results]

    return summary


def emit_chain_metric(summary: dict[str, Any]) -> bool:
    """Publish the break count where the monitoring stack can see it.

    Metadata only, never row content: a Langfuse span or a Prometheus sample
    carrying an evidence span would put the transcript somewhere the audit
    tables' access controls do not reach. Table names, counts and booleans are
    the whole payload -- `head_hash` is deliberately left out, since a published
    head is a thing a tamperer can read to learn what they have to reproduce.

    Never raises. A monitoring outage must not stop a verification job from
    finishing and logging, which is the channel that still works when Langfuse
    does not. Returns whether the record was accepted, so the summary can say
    "verified but unreported" instead of implying an alert that never left.
    """
    from indic_platform.obs.langfuse import default_sink

    record = {
        "vendor": METRIC_VENDOR,
        "capability": METRIC_CAPABILITY,
        "model": METRIC_MODEL,
        "prompt_version": None,
        "latency_ms": 0.0,
        # No vendor was called, so there is nothing to bill. Zeroes rather than
        # omitted keys: the sink reads both, and a fabricated unit count would
        # pollute the cost dashboards this feeds.
        "units": {},
        "cost_inr": 0.0,
        "cost_usd": 0.0,
        "status": "broken" if summary["breaks"] else "ok",
        "event": CHAIN_BREAKS_METRIC,
        "value": summary["breaks"],
        "rows": summary["rows"],
        "tables": {t["table"]: t["ok"] for t in summary["tables"]},
        "unanchored": list(summary.get("unanchored", [])),
    }
    try:
        default_sink().emit(record)
    except Exception:
        # Local log only: the span rules forbid tracing exception text to the
        # vendor, and this one is about the vendor being unreachable anyway.
        log.exception("uc3 audit chain metric was not published")
        return False
    return True
