"""Review-quality metrics for the reviewer, lead and governance dashboards (PRD E7).

Two numbers, and both of them are easy to get quietly wrong.

**Precision** is `confirmed / (confirmed + false_positive)` over flags, where the
decision is a reviewer's, not the detector's. Three rules make it mean that:

1. *Only the latest disposition per flag counts.* The dispositions table is
   append-only and hash-chained, so a reviewer who changes their mind writes a
   second row rather than editing the first. The chain keeps both; precision
   reflects the current decision. "Latest" is by `seq`, never by `created_at`:
   every row written in one transaction shares a statement timestamp, so
   `created_at` cannot order two dispositions appended together, while `seq` is
   `generated always as identity` and totally ordered.
2. *`needs_more_context` and `escalated` are not decisions.* They are a reviewer
   saying "not yet" and "not mine". Counting them as false positives would
   punish the detector for a reviewer's hesitation; counting them as confirmed
   would flatter it. They are in neither the numerator nor the denominator, and
   a flag whose *latest* disposition is one of them leaves the denominator again
   even if an earlier row had decided it.
3. *A zero denominator is `None`, never a number.* A category nobody has decided
   a flag in has no precision -- not 1.0, not 0.0. The uc3/P1 review caught this
   exact defect shipping as `evidence_failure_rate: 0.0` over zero checks, and
   CLAUDE.md is blunt about it: missing data is reported as "unmeasured", never
   given a passing placeholder. Every rate in this module is `float | None` for
   that reason, and the `None` is load-bearing.

**The false-negative estimate** is the harder one, because nothing in this system
knows what it missed. What it does have is E5's daily random QA sample: calls
that neither the lexicon nor the triage score escalated are sampled at
`qa_sample_rate` and sent through Stage 2 anyway. `detector.combine` adds the
`qa_sample` reason *only* when no other reason fired, so the sample is a clean
random draw from the calls the routing rule would have dropped. That reason is
persisted in `analysis_runs.output["escalation_reasons"]` -- there is no
`qa_sampled` column anywhere, and this JSONB array is the only record that a
call was sampled.

So the estimate this module computes is, precisely:

    fraction of sampled-but-not-escalated calls that a reviewer confirmed
    contained at least one genuine finding

with the denominator restricted to sampled calls that are *settled*: every flag
on the call has a decided latest disposition (a call with no flags is settled
vacuously). A sampled call still carrying an undecided flag is `pending` and is
excluded from both numerator and denominator, by the same discipline as rule 2
above -- see `false_negative_breakdown` for those counts.

Its honest limitation, which the report and the dashboard should carry: the
*negative* side of that denominator rests on Stage 2 finding nothing, not on a
human re-reading the call. Nobody reviews a sampled call that produced no flags,
because this system only ever puts flags in front of a reviewer. The number is
therefore a lower bound on the true miss rate: it measures what the routing rule
(lexicon + triage theta) misses that deep analysis plus a reviewer would catch,
and it cannot see anything all three miss. Dropping the flagless calls instead
would not fix that -- it would turn the metric into precision over the QA stream
and stop being a false-negative rate at all.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from indic_platform.db.models import AnalysisRun, Disposition, Flag
from sqlalchemy import Join, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from comms_surveillance.detector import CATEGORIES

# The two dispositions that decide a flag, and the two that do not. Exported
# vocabulary: the API layer classifies the same way, and a disposition outside
# either set -- a fifth value added later without touching this module -- counts
# as undecided, which keeps it out of every denominator until someone says where
# it belongs.
CONFIRMED = "confirmed"
FALSE_POSITIVE = "false_positive"
DECIDED: frozenset[str] = frozenset({CONFIRMED, FALSE_POSITIVE})
UNDECIDED: frozenset[str] = frozenset({"needs_more_context", "escalated"})

# The marker `detector.combine` writes when a call was escalated for no reason
# other than the daily random sample.
QA_SAMPLE_REASON = "qa_sample"

# Category order for every listing, so two calls of the same endpoint agree.
# Categories found in the data but absent from the policy vocabulary (a flag
# written under an older lexicon) are appended, sorted, rather than dropped.
CATEGORY_ORDER: tuple[str, ...] = tuple(CATEGORIES)

BUCKETS = ("day", "week", "month")


@dataclass(frozen=True)
class CategoryPrecision:
    """Reviewer-decided precision for one category over one window."""

    category: str
    confirmed: int
    false_positive: int
    decided: int
    precision: float | None

    @property
    def unmeasured(self) -> bool:
        return self.precision is None


@dataclass(frozen=True)
class FalseNegativeEstimate:
    """`missed / settled` over the QA sample. See the module docstring.

    Every field counts *calls*, not flags: the sample is drawn per call, so a
    call with three confirmed flags is one miss, not three.

    `sampled` is the whole sample. `settled` is the part of it a reviewer has
    finished with, and is the denominator. They are separate fields because an
    earlier version of this dataclass had only `sampled`, set to the settled
    count -- so a programme with 100 sampled calls and 98 still in the queue
    reported "sampled 2, missed 0, rate 0.0%" and looked like a clean bill of
    health over a sample almost none of which had been reviewed. A denominator
    that silently shrinks is the same lie as a zero denominator reported as a
    number, and CLAUDE.md forbids both.

    `flagless` is the part of `settled` that no reviewer ever actually looked
    at: this system only puts flags in front of people, so a sampled call that
    raised nothing is counted clean on the detector's own word. It is carried
    here so the caveat travels with the number instead of living in a docstring.
    """

    sampled: int
    settled: int
    missed: int
    pending: int
    flagless: int
    rate: float | None

    @property
    def unmeasured(self) -> bool:
        return self.rate is None


def _ratio(numerator: int, denominator: int) -> float | None:
    """The one place a rate is computed, so there is one place to get it wrong.

    `None` on a zero denominator -- never 0.0, never 1.0.
    """
    if denominator <= 0:
        return None
    return numerator / denominator


# --- time ----------------------------------------------------------------------
#
# Everything here is UTC and every window is half-open, `[start, end)`. A naive
# datetime is read as UTC rather than rejected: the callers are an API query
# string and a Celery beat job, and guessing the server's local zone is how a
# week's flags end up in the wrong bucket twice a year.


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_utc_opt(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _within(when: datetime, since: datetime | None, until: datetime | None) -> bool:
    """Half-open `[since, until)`, applied to already-UTC values."""
    if since is not None and when < since:
        return False
    return not (until is not None and when >= until)


def bucket_start(when: datetime, bucket: str) -> datetime:
    """The start of the UTC bucket `when` falls in.

    Weeks start Monday 00:00:00 UTC (ISO-8601), months on the 1st.
    """
    moment = as_utc(when)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "day":
        return midnight
    if bucket == "week":
        return midnight - timedelta(days=midnight.weekday())
    if bucket == "month":
        return midnight.replace(day=1)
    raise ValueError(f"unknown bucket {bucket!r}; expected one of {', '.join(BUCKETS)}")


def bucket_end(start: datetime, bucket: str) -> datetime:
    """The exclusive end of the bucket beginning at `start`."""
    if bucket == "day":
        return start + timedelta(days=1)
    if bucket == "week":
        return start + timedelta(days=7)
    if bucket == "month":
        return (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    raise ValueError(f"unknown bucket {bucket!r}; expected one of {', '.join(BUCKETS)}")


# --- the fold from dispositions to one decision per flag ------------------------


@dataclass(frozen=True)
class _Observation:
    """One `(flag, disposition)` pair as the join returns it.

    `disposition` and `seq` are `None` for a flag nobody has dispositioned yet:
    the join is an outer one, because a flag with no disposition is exactly the
    thing that must not silently vanish from a count of what is undecided.
    """

    flag_id: Any
    category: str
    created_at: datetime
    disposition: str | None
    seq: int | None


@dataclass(frozen=True)
class _Decision:
    """A flag reduced to its latest disposition."""

    flag_id: Any
    category: str
    created_at: datetime
    disposition: str | None

    @property
    def is_decided(self) -> bool:
        return self.disposition in DECIDED


def latest_decisions(observations: Iterable[_Observation]) -> list[_Decision]:
    """Fold `(flag, disposition)` pairs down to one decision per flag, by `seq`.

    Deliberately in Python rather than a SQL window function: this rule is the
    whole semantics of the metric, the volume is bounded by dispositions humans
    have actually written, and a rule implemented in a `ROW_NUMBER()` clause is
    a rule no unit test in this sandbox can reach. The statements below still
    narrow the scan to the window being reported.
    """
    best: dict[Any, tuple[int, _Observation]] = {}
    seen: dict[Any, _Observation] = {}
    for observation in observations:
        seen.setdefault(observation.flag_id, observation)
        if observation.disposition is None or observation.seq is None:
            continue
        current = best.get(observation.flag_id)
        if current is None or observation.seq > current[0]:
            best[observation.flag_id] = (observation.seq, observation)

    decisions = []
    for flag_id, observation in seen.items():
        winner = best.get(flag_id)
        decisions.append(
            _Decision(
                flag_id=flag_id,
                category=observation.category,
                created_at=observation.created_at,
                disposition=winner[1].disposition if winner else None,
            )
        )
    return decisions


def _tally(decisions: Iterable[_Decision]) -> dict[str, dict[str, int]]:
    """Per category: confirmed, false_positive, and how many flags were raised."""
    counts: dict[str, dict[str, int]] = {}
    for decision in decisions:
        bucket = counts.setdefault(decision.category, {CONFIRMED: 0, FALSE_POSITIVE: 0, "flags": 0})
        bucket["flags"] += 1
        if decision.disposition in DECIDED:
            bucket[str(decision.disposition)] += 1
    return counts


def _ordered_categories(found: Iterable[str]) -> list[str]:
    extra = sorted(set(found) - set(CATEGORY_ORDER))
    return [*CATEGORY_ORDER, *extra]


def _category_precision(category: str, counts: dict[str, int] | None) -> CategoryPrecision:
    confirmed = counts[CONFIRMED] if counts else 0
    false_positive = counts[FALSE_POSITIVE] if counts else 0
    decided = confirmed + false_positive
    return CategoryPrecision(
        category=category,
        confirmed=confirmed,
        false_positive=false_positive,
        decided=decided,
        precision=_ratio(confirmed, decided),
    )


# --- queries --------------------------------------------------------------------


#: The marker `comms_surveillance.demo_seed` writes onto every `analysis_runs`
#: row it creates. Flags hanging off such a run -- and the dispositions written
#: against them -- are demonstration data: a seeded `confirmed` is a fabricated
#: human verdict, and counting one would put an invented precision on the
#: governance dashboard beside the real measured figures. That is the passing
#: placeholder CLAUDE.md forbids, and it would be shown to exactly the audience
#: the demo exists for.
#:
#: So every statement below excludes them, and `demo_row_counts` reports how
#: many were left out: a silent exclusion is its own kind of untruth, and an
#: operator looking at an empty dashboard needs to be able to tell "nothing
#: happened" from "everything here was demo data".
DEMO_MARKER: dict[str, Any] = {"demo": True}


def _tables_in(froms: Iterable[Any]) -> set[Any]:
    """Every table a statement selects from, joins included.

    `get_final_froms` returns one `Join` for a joined statement rather than its
    operands, so a membership test against it reports "not joined" for a
    statement that plainly is. The walk is explicit because the alternative --
    trusting the caller -- is what `demo_free` exists to stop trusting.
    """
    found: set[Any] = set()
    for element in froms:
        if isinstance(element, Join):
            found |= _tables_in([element.left, element.right])
        else:
            found.add(element)
    return found


def demo_free(statement: Select[Any]) -> Select[Any]:
    """Drop rows produced by the demo seeder. See `DEMO_MARKER`.

    `~contains` rather than a `demo = false` test: a pipeline row has no `demo`
    key at all, and an equality test against a missing key is null, not true.

    Public because `api.queue_statement` needs it for the QA-sample stream,
    which builds its own join to `analysis_runs` rather than going through any
    statement here. Every place that *measures* must apply this; the reviewer's
    queue must not, because showing the seeded flags is the whole point of them.

    The join is required rather than added. Adding it would hide the topology
    from the caller, and applied to a statement that has not joined
    `analysis_runs` the bare `where` silently produces a cartesian product --
    no SQLAlchemy warning, a compiled query that still mentions `analysis_runs`,
    and numbers multiplied by the row count of a table designed only to grow.
    Loud here beats wrong there.
    """
    if AnalysisRun.__table__ not in _tables_in(statement.get_final_froms()):
        raise ValueError(
            "demo_free needs the statement to join analysis_runs; applied without it "
            "the filter becomes a cross join"
        )
    return statement.where(~AnalysisRun.output.contains(DEMO_MARKER))


async def demo_row_counts(session: AsyncSession) -> dict[str, int]:
    """How much demonstration data this database holds, so the exclusion is visible."""
    runs = await session.scalar(
        select(func.count())
        .select_from(AnalysisRun)
        .where(AnalysisRun.output.contains(DEMO_MARKER))
    )
    flags = await session.scalar(
        select(func.count())
        .select_from(Flag)
        .join(AnalysisRun, AnalysisRun.id == Flag.run_id)
        .where(AnalysisRun.output.contains(DEMO_MARKER))
    )
    return {"analysis_runs": int(runs or 0), "flags": int(flags or 0)}


def flag_statement(since: datetime | None, until: datetime | None) -> Select[Any]:
    """Flags in `[since, until)` with every disposition written against them.

    Windowed on `flags.created_at` -- when the *detector* raised the flag, not
    when a reviewer got to it. Precision over time is a statement about the
    detector, and bucketing by decision time would smear one week's model
    behaviour across however long the queue took to drain.
    """
    statement = demo_free(
        select(Flag.id, Flag.category, Flag.created_at, Disposition.disposition, Disposition.seq)
        .select_from(Flag)
        # An inner join: `flags.run_id` is a non-nullable foreign key, so every
        # flag has exactly one run and none is lost by joining to it.
        .join(AnalysisRun, AnalysisRun.id == Flag.run_id)
        .outerjoin(Disposition, Disposition.flag_id == Flag.id)
    )
    if since is not None:
        statement = statement.where(Flag.created_at >= since)
    if until is not None:
        statement = statement.where(Flag.created_at < until)
    return statement


def qa_sample_statement(since: datetime | None) -> Select[Any]:
    """QA-sampled analysis runs, their flags, and every disposition on them.

    The `@>` containment is on the whole `output` document so a GIN index on
    that JSONB column can serve it. The predicate is re-applied in Python by
    `_qa_calls`, which is what the unit tests exercise; this one is the
    index-friendly narrowing, not the definition.
    """
    statement = (
        select(
            AnalysisRun.id,
            AnalysisRun.call_id,
            AnalysisRun.output,
            AnalysisRun.created_at,
            Flag.id,
            Disposition.disposition,
            Disposition.seq,
        )
        .select_from(AnalysisRun)
        .outerjoin(Flag, Flag.run_id == AnalysisRun.id)
        .outerjoin(Disposition, Disposition.flag_id == Flag.id)
        .where(AnalysisRun.output.contains({"escalation_reasons": [QA_SAMPLE_REASON]}))
    )
    statement = demo_free(statement)
    if since is not None:
        statement = statement.where(AnalysisRun.created_at >= since)
    return statement


async def _flag_observations(
    session: AsyncSession, *, since: datetime | None, until: datetime | None
) -> list[_Observation]:
    result = await session.execute(flag_statement(since, until))
    rows = [
        _Observation(
            flag_id=row[0],
            category=row[1],
            created_at=as_utc(row[2]),
            disposition=row[3],
            seq=row[4],
        )
        for row in result.all()
    ]
    # Re-applied rather than trusted to the WHERE clause: the boundary rule
    # (half-open, UTC) is one implementation, and it is this one.
    return [row for row in rows if _within(row.created_at, since, until)]


# --- precision ------------------------------------------------------------------


async def precision_by_category(
    session: AsyncSession,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[CategoryPrecision]:
    """Reviewer-decided precision per category over `[since, until)`.

    Every policy category is returned, whether or not it has data: one with no
    decided flag comes back `decided=0, precision=None` -- unmeasured, which is
    a different and more useful statement than a silent omission.
    """
    since, until = _as_utc_opt(since), _as_utc_opt(until)
    decisions = latest_decisions(await _flag_observations(session, since=since, until=until))
    counts = _tally(decisions)
    return [
        _category_precision(category, counts.get(category))
        for category in _ordered_categories(counts)
    ]


async def precision_over_time(
    session: AsyncSession,
    *,
    bucket: str = "week",
    since: datetime | None = None,
) -> list[dict[str, Any]]:
    """Per-category precision per time bucket, oldest bucket first.

    Buckets are UTC and half-open: `bucket_start <= flags.created_at <
    bucket_end`, weeks starting Monday 00:00:00 UTC. A `(bucket, category)` pair
    that raised flags but has none decided is still returned, with
    `precision=None` and a non-zero `flags` count -- "five flags, nobody decided"
    is a finding about the review queue, and dropping the row would hide it. A
    pair with no flags at all is not returned; there is nothing to say about it.

    An overall figure per bucket is deliberately not emitted: precisions cannot
    be averaged, and the counts needed to compute one correctly
    (`sum(confirmed) / sum(decided)`, `None` when that sum is zero) are all here.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}; expected one of {', '.join(BUCKETS)}")
    since = _as_utc_opt(since)
    decisions = latest_decisions(await _flag_observations(session, since=since, until=None))

    grouped: dict[datetime, list[_Decision]] = {}
    for decision in decisions:
        grouped.setdefault(bucket_start(decision.created_at, bucket), []).append(decision)

    rows: list[dict[str, Any]] = []
    for start in sorted(grouped):
        counts = _tally(grouped[start])
        for category in _ordered_categories(counts):
            if category not in counts:
                continue
            measured = _category_precision(category, counts[category])
            rows.append(
                {
                    "bucket": bucket,
                    "bucket_start": start,
                    "bucket_end": bucket_end(start, bucket),
                    "category": measured.category,
                    "flags": counts[category]["flags"],
                    "confirmed": measured.confirmed,
                    "false_positive": measured.false_positive,
                    "decided": measured.decided,
                    "precision": measured.precision,
                }
            )
    return rows


# --- false negatives -------------------------------------------------------------


@dataclass(frozen=True)
class _QaCall:
    """One sampled call, reduced to what its flags were decided as."""

    call_id: Any
    decisions: tuple[_Decision, ...]

    @property
    def missed(self) -> bool:
        """A reviewer confirmed something on a call the routing rule dropped."""
        return any(d.disposition == CONFIRMED for d in self.decisions)

    @property
    def pending(self) -> bool:
        """Some flag here is still undecided, so the call has not been settled."""
        return not self.missed and any(not d.is_decided for d in self.decisions)

    @property
    def flagless(self) -> bool:
        """Stage 2 found nothing. No human ever looked -- see the module docstring."""
        return not self.decisions


def _qa_calls(rows: Sequence[Any], *, since: datetime | None) -> list[_QaCall]:
    """Group the join into one entry per sampled call.

    Grouped by `call_id`, not by run: `detector.qa_sampled` draws on the call id,
    so a call re-analysed twice in a window is one sampled call, not two.
    """
    observations: dict[Any, dict[Any, list[_Observation]]] = {}
    for row in rows:
        call_id, output, created_at, flag_id = row[1], row[2], row[3], row[4]
        reasons = (output or {}).get("escalation_reasons") or []
        if QA_SAMPLE_REASON not in reasons:
            continue
        if not _within(as_utc(created_at), since, None):
            continue
        per_call = observations.setdefault(call_id, {})
        if flag_id is None:
            continue
        per_call.setdefault(flag_id, []).append(
            _Observation(
                flag_id=flag_id,
                # `flags.category` is not selected: the estimate is per call,
                # and a call is one miss whatever its flags were about. A
                # per-category breakdown of misses is available from
                # `flags.category` if it is ever asked for; it is not here
                # because nothing has asked.
                category="",
                created_at=as_utc(created_at),
                disposition=row[5],
                seq=row[6],
            )
        )

    calls = []
    for call_id, per_flag in observations.items():
        flat = [obs for group in per_flag.values() for obs in group]
        calls.append(_QaCall(call_id=call_id, decisions=tuple(latest_decisions(flat))))
    return calls


async def _qa_sample(session: AsyncSession, *, since: datetime | None) -> list[_QaCall]:
    result = await session.execute(qa_sample_statement(since))
    return _qa_calls(result.all(), since=since)


async def false_negative_estimate(
    session: AsyncSession, *, since: datetime | None = None
) -> FalseNegativeEstimate:
    """How often the routing rule dropped a call that actually contained something.

    Denominator: QA-sampled calls that are settled (every flag decided, or no
    flags at all). Numerator: those with at least one `confirmed` flag. A sampled
    call still holding an undecided flag counts in neither -- it is reported by
    `false_negative_breakdown` as `pending`, not rounded into the clean side.

    `settled == 0` -- no sample yet, or none of it reviewed -- returns
    `rate=None`. There is no denominator, so there is no number, and inventing
    one would be the placeholder CLAUDE.md forbids.
    """
    calls = await _qa_sample(session, since=_as_utc_opt(since))
    settled = [call for call in calls if not call.pending]
    missed = sum(1 for call in settled if call.missed)
    return FalseNegativeEstimate(
        sampled=len(calls),
        settled=len(settled),
        missed=missed,
        pending=len(calls) - len(settled),
        flagless=sum(1 for call in settled if call.flagless),
        rate=_ratio(missed, len(settled)),
    )


async def false_negative_breakdown(
    session: AsyncSession, *, since: datetime | None = None
) -> dict[str, int]:
    """The counts behind `false_negative_estimate`, including what it excluded.

    Additive to the pinned contract and not required by it: the estimate's
    denominator drops `pending` calls and leans on `flagless` ones, and a metric
    that hides either of those is a metric nobody can argue with.
    """
    calls = await _qa_sample(session, since=_as_utc_opt(since))
    pending = [call for call in calls if call.pending]
    settled = [call for call in calls if not call.pending]
    return {
        "calls": len(calls),
        "settled": len(settled),
        "missed": sum(1 for call in settled if call.missed),
        "clean": sum(1 for call in settled if not call.missed),
        "flagless": sum(1 for call in settled if call.flagless),
        "pending": len(pending),
    }


__all__ = [
    "BUCKETS",
    "CATEGORY_ORDER",
    "CategoryPrecision",
    "FalseNegativeEstimate",
    "bucket_end",
    "bucket_start",
    "false_negative_breakdown",
    "false_negative_estimate",
    "precision_by_category",
    "precision_over_time",
]
