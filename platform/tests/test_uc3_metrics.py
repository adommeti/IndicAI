"""Precision and false-negative metrics (uc3/P6, PRD E7).

The tests that matter most here are the ones about *absence*. A metrics module
is the easiest place in a codebase to ship a lie, because a lie looks like a
number and a missing measurement looks like a failure. uc3/P1's review caught
exactly that shape -- `evidence_failure_rate: 0.0` computed over zero checks --
so the zero-denominator cases come first and are covered hardest: every path
that could produce a precision or a rate with nothing behind it is pinned to
`None`, and pinned as `is None` rather than falsily, because `0.0` would pass a
truthiness assertion.

After that, the two rules that make precision mean "what the reviewer currently
thinks": only the latest disposition per flag counts (by `seq`, not by
`created_at`), and `needs_more_context`/`escalated` are not decisions at all.

No Postgres in this sandbox, so the aggregation is driven through a fake session
returning the rows the join would. That is not a substitute for the SQL being
right, so the statements are also compiled against the real PostgreSQL dialect
(a pure test -- no connection), and one end-to-end test against a live database
is marked `integration`.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from comms_surveillance import metrics
from sqlalchemy.dialects import postgresql

CATEGORY = "guaranteed_returns"
OTHER = "mnpi_insider"

WEEK = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)  # a Monday
MONDAY = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)


# --- test doubles ----------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class FakeSession:
    """An `AsyncSession` stand-in that answers one `execute` with canned rows.

    Each public metric issues exactly one statement, so this is enough to drive
    the whole fold. It deliberately ignores the WHERE clause: the window and the
    QA-sample predicate are re-applied in Python precisely so they are testable
    without a database, and a fake that filtered too would test itself.
    """

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows)


def flag_row(
    flag_id: Any,
    category: str = CATEGORY,
    created_at: datetime = WEEK,
    disposition: str | None = None,
    seq: int | None = None,
) -> tuple[Any, ...]:
    """One row of `flags LEFT JOIN dispositions`."""
    return (flag_id, category, created_at, disposition, seq)


def qa_row(
    call_id: Any,
    *,
    reasons: tuple[str, ...] = ("qa_sample",),
    created_at: datetime = WEEK,
    flag_id: Any = None,
    disposition: str | None = None,
    seq: int | None = None,
    run_id: Any = None,
) -> tuple[Any, ...]:
    """One row of `analysis_runs LEFT JOIN flags LEFT JOIN dispositions`."""
    return (
        run_id or uuid.uuid4(),
        call_id,
        {"escalation_reasons": list(reasons)},
        created_at,
        flag_id,
        disposition,
        seq,
    )


async def precision(rows: list[tuple[Any, ...]], **kwargs: Any) -> dict[str, Any]:
    result = await metrics.precision_by_category(FakeSession(rows), **kwargs)  # type: ignore[arg-type]
    return {row.category: row for row in result}


# =================================================================================
# Zero denominators. Nothing else in this file matters if these are wrong.
# =================================================================================


async def test_a_category_with_no_flags_at_all_is_unmeasured_not_perfect() -> None:
    by_category = await precision([])
    assert set(by_category) == set(metrics.CATEGORY_ORDER)
    for row in by_category.values():
        assert row.precision is None, f"{row.category} invented a precision from no data"
        assert row.decided == 0
        assert row.unmeasured is True


async def test_flags_with_no_dispositions_are_unmeasured_not_zero() -> None:
    """Five raised flags and no reviewer decisions is not precision 0.0."""
    rows = [flag_row(uuid.uuid4()) for _ in range(5)]
    row = (await precision(rows))[CATEGORY]
    assert row.precision is None
    assert (row.confirmed, row.false_positive, row.decided) == (0, 0, 0)


async def test_only_undecided_dispositions_leave_the_category_unmeasured() -> None:
    rows = [
        flag_row(uuid.uuid4(), disposition="needs_more_context", seq=1),
        flag_row(uuid.uuid4(), disposition="escalated", seq=2),
    ]
    row = (await precision(rows))[CATEGORY]
    assert row.precision is None
    assert row.decided == 0


async def test_a_decided_flag_outside_the_window_does_not_rescue_the_window() -> None:
    """Filtering must not leave the counts behind and report a stale rate."""
    rows = [
        flag_row(uuid.uuid4(), created_at=WEEK - timedelta(days=30), disposition="confirmed", seq=1)
    ]
    row = (await precision(rows, since=WEEK))[CATEGORY]
    assert row.precision is None
    assert row.decided == 0


async def test_the_ratio_helper_never_returns_a_number_for_an_empty_denominator() -> None:
    assert metrics._ratio(0, 0) is None
    assert metrics._ratio(3, 0) is None
    assert metrics._ratio(0, 3) == 0.0


async def test_false_negative_rate_is_none_when_nothing_was_sampled() -> None:
    estimate = await metrics.false_negative_estimate(FakeSession([]))  # type: ignore[arg-type]
    assert estimate.sampled == 0
    assert estimate.missed == 0
    assert estimate.rate is None
    assert estimate.unmeasured is True


async def test_false_negative_rate_is_none_when_the_whole_sample_is_pending() -> None:
    """Sampled calls whose flags nobody has decided give no denominator at all."""
    rows = [
        qa_row("call-a", flag_id="f1"),
        qa_row("call-b", flag_id="f2", disposition="needs_more_context", seq=1),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert estimate.sampled == 0
    assert estimate.missed == 0
    assert estimate.rate is None

    breakdown = await metrics.false_negative_breakdown(FakeSession(rows))  # type: ignore[arg-type]
    assert breakdown == {
        "calls": 2,
        "sampled": 0,
        "missed": 0,
        "clean": 0,
        "flagless": 0,
        "pending": 2,
    }


async def test_only_non_qa_runs_in_the_data_is_unmeasured_not_a_clean_bill() -> None:
    """A window with escalated-on-merit runs and no sample has no estimate."""
    rows = [
        qa_row("call-a", reasons=("lexicon:high",), flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-b", reasons=("triage:7>=theta:5",), flag_id="f2"),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (0, 0, None)


async def test_an_over_time_bucket_with_no_decisions_reports_none_not_zero() -> None:
    rows = [flag_row(uuid.uuid4()), flag_row(uuid.uuid4())]
    buckets = await metrics.precision_over_time(FakeSession(rows))  # type: ignore[arg-type]
    assert len(buckets) == 1
    assert buckets[0]["precision"] is None
    assert buckets[0]["decided"] == 0
    # The row is still emitted: two flags nobody decided is a fact about the
    # review queue, and an omitted bucket would read as "no flags that week".
    assert buckets[0]["flags"] == 2


async def test_no_metric_reports_one_point_zero_off_an_empty_set() -> None:
    """The failure mode is symmetric: 1.0 is as much a lie as 0.0."""
    by_category = await precision([flag_row(uuid.uuid4(), disposition="escalated", seq=1)])
    assert all(row.precision != 1.0 for row in by_category.values())
    assert by_category[CATEGORY].precision is None


# =================================================================================
# Latest-disposition-wins
# =================================================================================


async def test_a_superseded_disposition_does_not_double_count() -> None:
    """One flag, two dispositions: one decided flag, not two."""
    flag = uuid.uuid4()
    rows = [
        flag_row(flag, disposition="confirmed", seq=1),
        flag_row(flag, disposition="false_positive", seq=2),
    ]
    row = (await precision(rows))[CATEGORY]
    assert row.decided == 1
    assert (row.confirmed, row.false_positive) == (0, 1)
    assert row.precision == 0.0


async def test_latest_is_by_seq_not_by_row_order() -> None:
    """The join returns no order; the fold must not depend on one."""
    flag = uuid.uuid4()
    forwards = [
        flag_row(flag, disposition="false_positive", seq=10),
        flag_row(flag, disposition="confirmed", seq=11),
    ]
    backwards = list(reversed(forwards))
    assert (await precision(forwards))[CATEGORY].confirmed == 1
    assert (await precision(backwards))[CATEGORY].confirmed == 1


async def test_latest_is_by_seq_not_by_created_at() -> None:
    """Rows written in one transaction share a timestamp, so `seq` decides.

    Here both dispositions carry the same `created_at` and only `seq`
    distinguishes them. A fold that ordered by timestamp would pick arbitrarily.
    """
    flag = uuid.uuid4()
    rows = [
        flag_row(flag, disposition="confirmed", seq=41),
        flag_row(flag, disposition="false_positive", seq=42),
    ]
    row = (await precision(rows))[CATEGORY]
    assert row.false_positive == 1 and row.confirmed == 0


async def test_a_reviewer_changing_their_mind_back_lands_on_the_last_word() -> None:
    flag = uuid.uuid4()
    rows = [
        flag_row(flag, disposition="confirmed", seq=1),
        flag_row(flag, disposition="false_positive", seq=2),
        flag_row(flag, disposition="confirmed", seq=3),
    ]
    row = (await precision(rows))[CATEGORY]
    assert (row.confirmed, row.false_positive, row.decided) == (1, 0, 1)
    assert row.precision == 1.0


# =================================================================================
# needs_more_context / escalated never move precision
# =================================================================================


async def test_needs_more_context_does_not_move_precision() -> None:
    decided = [
        flag_row(uuid.uuid4(), disposition="confirmed", seq=1),
        flag_row(uuid.uuid4(), disposition="confirmed", seq=2),
        flag_row(uuid.uuid4(), disposition="false_positive", seq=3),
    ]
    before = (await precision(decided))[CATEGORY]
    after = (
        await precision([*decided, flag_row(uuid.uuid4(), disposition="needs_more_context", seq=4)])
    )[CATEGORY]
    assert before.precision == pytest.approx(2 / 3)
    assert after.precision == before.precision
    assert after.decided == before.decided == 3
    # The fourth flag exists; it simply is not a decision.
    assert after.confirmed + after.false_positive == 3


async def test_escalated_does_not_move_precision() -> None:
    decided = [flag_row(uuid.uuid4(), disposition="confirmed", seq=1)]
    after = (await precision([*decided, flag_row(uuid.uuid4(), disposition="escalated", seq=2)]))[
        CATEGORY
    ]
    assert (after.confirmed, after.false_positive, after.decided) == (1, 0, 1)
    assert after.precision == 1.0


async def test_a_disposition_this_module_does_not_know_is_not_a_decision() -> None:
    """A fifth disposition added to the schema must not land in a denominator.

    Defaulting an unrecognised value into `decided` would let a future migration
    change every historical precision figure without anyone editing this file.
    """
    rows = [
        flag_row(uuid.uuid4(), disposition="confirmed", seq=1),
        flag_row(uuid.uuid4(), disposition="referred_to_legal", seq=2),
    ]
    row = (await precision(rows))[CATEGORY]
    assert (row.confirmed, row.false_positive, row.decided) == (1, 0, 1)


async def test_needs_more_context_superseding_a_decision_removes_it_again() -> None:
    """Re-opening a flag takes it back out of the denominator.

    "Only the latest counts" and "`needs_more_context` is not a decision" compose:
    a reviewer who confirms and then reopens has withdrawn the decision, and the
    flag is undecided again rather than frozen at its last decided value.
    """
    flag = uuid.uuid4()
    rows = [
        flag_row(flag, disposition="confirmed", seq=1),
        flag_row(flag, disposition="needs_more_context", seq=2),
    ]
    row = (await precision(rows))[CATEGORY]
    assert row.decided == 0
    assert row.precision is None


# =================================================================================
# Precision arithmetic, categories and windows
# =================================================================================


async def test_precision_is_confirmed_over_decided() -> None:
    rows = [
        *[flag_row(uuid.uuid4(), disposition="confirmed", seq=i) for i in range(3)],
        flag_row(uuid.uuid4(), disposition="false_positive", seq=9),
    ]
    row = (await precision(rows))[CATEGORY]
    assert (row.confirmed, row.false_positive, row.decided) == (3, 1, 4)
    assert row.precision == 0.75


async def test_categories_are_scored_separately() -> None:
    rows = [
        flag_row(uuid.uuid4(), CATEGORY, disposition="confirmed", seq=1),
        flag_row(uuid.uuid4(), OTHER, disposition="false_positive", seq=2),
    ]
    by_category = await precision(rows)
    assert by_category[CATEGORY].precision == 1.0
    assert by_category[OTHER].precision == 0.0
    assert by_category["conduct"].precision is None


async def test_every_policy_category_is_listed_in_a_stable_order() -> None:
    result = await metrics.precision_by_category(FakeSession([]))  # type: ignore[arg-type]
    assert [row.category for row in result] == list(metrics.CATEGORY_ORDER)
    assert "instruction_like_content" in metrics.CATEGORY_ORDER


async def test_a_category_outside_the_policy_vocabulary_is_reported_not_dropped() -> None:
    """A flag written under an older lexicon still has to be counted somewhere."""
    rows = [flag_row(uuid.uuid4(), "retired_category", disposition="confirmed", seq=1)]
    result = await metrics.precision_by_category(FakeSession(rows))  # type: ignore[arg-type]
    assert [row.category for row in result][-1] == "retired_category"
    assert result[-1].precision == 1.0


async def test_the_window_is_half_open_in_utc() -> None:
    """`since` includes its own instant; `until` excludes it."""
    at_since = flag_row(uuid.uuid4(), created_at=WEEK, disposition="confirmed", seq=1)
    at_until = flag_row(
        uuid.uuid4(), created_at=WEEK + timedelta(days=7), disposition="false_positive", seq=2
    )
    row = (await precision([at_since, at_until], since=WEEK, until=WEEK + timedelta(days=7)))[
        CATEGORY
    ]
    assert (row.confirmed, row.false_positive, row.decided) == (1, 0, 1)


async def test_naive_datetimes_are_read_as_utc_on_both_sides() -> None:
    naive_row = flag_row(
        uuid.uuid4(), created_at=datetime(2026, 9, 7, 12, 0), disposition="confirmed", seq=1
    )
    row = (await precision([naive_row], since=datetime(2026, 9, 7, 0, 0)))[CATEGORY]
    assert row.confirmed == 1


async def test_an_aware_non_utc_row_is_converted_before_it_is_bucketed() -> None:
    """A row an hour ahead of UTC midnight belongs to the UTC day, not the local one."""
    plus_two = datetime(2026, 9, 14, 1, 0, tzinfo=timezone_of(2))
    buckets = await metrics.precision_over_time(
        FakeSession([flag_row(uuid.uuid4(), created_at=plus_two)]),
        bucket="day",  # type: ignore[arg-type]
    )
    assert buckets[0]["bucket_start"] == datetime(2026, 9, 13, 0, 0, tzinfo=UTC)


def timezone_of(hours: int) -> Any:
    from datetime import timezone

    return timezone(timedelta(hours=hours))


# =================================================================================
# Buckets
# =================================================================================


def test_weeks_start_monday_midnight_utc() -> None:
    sunday_late = datetime(2026, 9, 6, 23, 59, 59, tzinfo=UTC)
    monday_early = datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)
    assert metrics.bucket_start(sunday_late, "week") == datetime(2026, 8, 31, tzinfo=UTC)
    assert metrics.bucket_start(monday_early, "week") == MONDAY
    assert metrics.bucket_end(MONDAY, "week") == datetime(2026, 9, 14, tzinfo=UTC)


def test_day_and_month_buckets() -> None:
    assert metrics.bucket_start(WEEK, "day") == datetime(2026, 9, 7, tzinfo=UTC)
    assert metrics.bucket_end(datetime(2026, 9, 7, tzinfo=UTC), "day") == datetime(
        2026, 9, 8, tzinfo=UTC
    )
    assert metrics.bucket_start(WEEK, "month") == datetime(2026, 9, 1, tzinfo=UTC)
    assert metrics.bucket_end(datetime(2026, 12, 1, tzinfo=UTC), "month") == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_an_unknown_bucket_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError, match="unknown bucket"):
        metrics.bucket_start(WEEK, "fortnight")
    with pytest.raises(ValueError, match="unknown bucket"):
        metrics.bucket_end(WEEK, "fortnight")


async def test_over_time_refuses_an_unknown_bucket_before_it_queries() -> None:
    session = FakeSession([])
    with pytest.raises(ValueError, match="unknown bucket"):
        await metrics.precision_over_time(session, bucket="quarter")  # type: ignore[arg-type]
    assert session.statements == []


async def test_over_time_buckets_are_half_open_and_ordered_oldest_first() -> None:
    rows = [
        flag_row(
            uuid.uuid4(),
            created_at=datetime(2026, 9, 6, 23, 59, tzinfo=UTC),
            disposition="confirmed",
            seq=1,
        ),
        # The first instant of the week starting Monday 2026-09-07 ...
        flag_row(uuid.uuid4(), created_at=MONDAY, disposition="false_positive", seq=2),
        # ... and the last one. Both belong to it.
        flag_row(
            uuid.uuid4(),
            created_at=datetime(2026, 9, 13, 23, 59, 59, tzinfo=UTC),
            disposition="confirmed",
            seq=3,
        ),
        # The next Monday opens a new bucket.
        flag_row(
            uuid.uuid4(),
            created_at=datetime(2026, 9, 14, 0, 0, tzinfo=UTC),
            disposition="confirmed",
            seq=4,
        ),
    ]
    buckets = await metrics.precision_over_time(FakeSession(rows))  # type: ignore[arg-type]
    starts = [row["bucket_start"] for row in buckets]
    assert starts == [
        datetime(2026, 8, 31, tzinfo=UTC),
        MONDAY,
        datetime(2026, 9, 14, tzinfo=UTC),
    ]
    assert [row["precision"] for row in buckets] == [1.0, 0.5, 1.0]
    assert buckets[0]["bucket_end"] == MONDAY
    assert buckets[1]["bucket_end"] == datetime(2026, 9, 14, tzinfo=UTC)
    assert all(row["bucket"] == "week" for row in buckets)


async def test_over_time_splits_categories_within_a_bucket() -> None:
    rows = [
        flag_row(uuid.uuid4(), CATEGORY, disposition="confirmed", seq=1),
        flag_row(uuid.uuid4(), OTHER, disposition="false_positive", seq=2),
    ]
    buckets = await metrics.precision_over_time(FakeSession(rows))  # type: ignore[arg-type]
    assert {row["category"]: row["precision"] for row in buckets} == {CATEGORY: 1.0, OTHER: 0.0}
    # A category with no flags in the bucket is absent rather than reported as 0.
    assert all(row["category"] != "conduct" for row in buckets)


async def test_over_time_counts_can_be_summed_into_an_overall_figure() -> None:
    """The module emits no overall row, so the counts must support one."""
    rows = [
        flag_row(uuid.uuid4(), CATEGORY, disposition="confirmed", seq=1),
        flag_row(uuid.uuid4(), OTHER, disposition="confirmed", seq=2),
        flag_row(uuid.uuid4(), OTHER, disposition="false_positive", seq=3),
    ]
    buckets = await metrics.precision_over_time(FakeSession(rows))  # type: ignore[arg-type]
    confirmed = sum(row["confirmed"] for row in buckets)
    decided = sum(row["decided"] for row in buckets)
    assert metrics._ratio(confirmed, decided) == pytest.approx(2 / 3)


# =================================================================================
# False-negative estimate
# =================================================================================


async def test_a_confirmed_flag_on_a_sampled_call_is_a_miss() -> None:
    rows = [
        qa_row("call-a", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-b"),  # sampled, Stage 2 found nothing
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed) == (2, 1)
    assert estimate.rate == 0.5


async def test_a_sampled_call_whose_flags_were_all_false_positives_is_clean() -> None:
    rows = [
        qa_row("call-a", flag_id="f1", disposition="false_positive", seq=1),
        qa_row("call-a", flag_id="f2", disposition="false_positive", seq=2),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (1, 0, 0.0)


async def test_misses_are_counted_per_call_not_per_flag() -> None:
    """The sample is drawn per call, so the denominator is calls; so is the numerator."""
    rows = [
        qa_row("call-a", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-a", flag_id="f2", disposition="confirmed", seq=2),
        qa_row("call-a", flag_id="f3", disposition="confirmed", seq=3),
        qa_row("call-b"),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed) == (2, 1)


async def test_a_call_analysed_twice_is_still_one_sampled_call() -> None:
    rows = [
        qa_row("call-a", run_id="run-1", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-a", run_id="run-2", flag_id="f2", disposition="confirmed", seq=2),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (1, 1, 1.0)


async def test_a_superseded_confirmation_on_the_sample_is_no_longer_a_miss() -> None:
    rows = [
        qa_row("call-a", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-a", flag_id="f1", disposition="false_positive", seq=2),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (1, 0, 0.0)


async def test_a_pending_call_is_excluded_from_both_sides() -> None:
    """One decided miss and one undecided call is 1/1, not 1/2 and not 2/2."""
    rows = [
        qa_row("call-a", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-b", flag_id="f2", disposition="needs_more_context", seq=2),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (1, 1, 1.0)

    breakdown = await metrics.false_negative_breakdown(FakeSession(rows))  # type: ignore[arg-type]
    assert breakdown["pending"] == 1
    assert breakdown["calls"] == 2


async def test_a_call_with_one_confirmed_and_one_undecided_flag_is_still_a_miss() -> None:
    """A confirmed finding settles the question the sample asks; the rest is detail."""
    rows = [
        qa_row("call-a", flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-a", flag_id="f2"),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed) == (1, 1)


async def test_a_call_escalated_on_its_merits_is_not_part_of_the_sample() -> None:
    """`qa_sample` is only written when nothing else escalated the call.

    Counting a lexicon-escalated call would contaminate the denominator with
    calls the routing rule caught, which is the opposite of what is being
    measured.
    """
    rows = [
        qa_row("call-a", reasons=("lexicon:high",), flag_id="f1", disposition="confirmed", seq=1),
        qa_row("call-b", flag_id="f2", disposition="confirmed", seq=2),
        qa_row("call-c"),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (2, 1, 0.5)


async def test_a_run_with_no_reasons_recorded_is_not_read_as_sampled() -> None:
    rows = [qa_row("call-a", reasons=(), flag_id="f1", disposition="confirmed", seq=1)]
    estimate = await metrics.false_negative_estimate(FakeSession(rows))  # type: ignore[arg-type]
    assert estimate.rate is None


async def test_the_sample_window_is_applied_to_the_analysis_run() -> None:
    rows = [
        qa_row(
            "old",
            created_at=WEEK - timedelta(days=30),
            flag_id="f1",
            disposition="confirmed",
            seq=1,
        ),
        qa_row("new", created_at=WEEK, flag_id="f2", disposition="false_positive", seq=2),
    ]
    estimate = await metrics.false_negative_estimate(FakeSession(rows), since=WEEK)  # type: ignore[arg-type]
    assert (estimate.sampled, estimate.missed, estimate.rate) == (1, 0, 0.0)


async def test_the_breakdown_separates_flagless_calls_from_reviewed_clean_ones() -> None:
    """Flagless sampled calls carry the estimate's weakest evidence; they are counted."""
    rows = [
        qa_row("call-a"),
        qa_row("call-b"),
        qa_row("call-c", flag_id="f1", disposition="false_positive", seq=1),
        qa_row("call-d", flag_id="f2", disposition="confirmed", seq=2),
    ]
    breakdown = await metrics.false_negative_breakdown(FakeSession(rows))  # type: ignore[arg-type]
    assert breakdown == {
        "calls": 4,
        "sampled": 4,
        "missed": 1,
        "clean": 3,
        "flagless": 2,
        "pending": 0,
    }


# =================================================================================
# The SQL itself (compiled, not executed)
# =================================================================================


def test_the_flag_statement_outer_joins_so_undecided_flags_survive() -> None:
    sql = str(metrics.flag_statement(None, None).compile(dialect=postgresql.dialect()))
    assert "LEFT OUTER JOIN dispositions" in sql
    assert "flags.created_at" in sql


def test_the_flag_statement_window_is_half_open() -> None:
    sql = str(
        metrics.flag_statement(WEEK, WEEK + timedelta(days=7)).compile(dialect=postgresql.dialect())
    )
    assert "flags.created_at >=" in sql
    assert "flags.created_at <" in sql
    assert "flags.created_at <=" not in sql


def test_the_qa_statement_matches_the_sample_by_jsonb_containment() -> None:
    compiled = metrics.qa_sample_statement(None).compile(dialect=postgresql.dialect())
    sql = str(compiled)
    # `@>` rather than a `->>` text comparison: containment is what a GIN index
    # on `analysis_runs.output` can answer.
    assert "@>" in sql
    # The predicate travels as a bound parameter, so it is checked there rather
    # than in the rendered SQL (JSONB has no literal renderer).
    assert {"escalation_reasons": [metrics.QA_SAMPLE_REASON]} in compiled.params.values()
    # Both joins are outer: a sampled call with no flags is the clean case, and
    # an inner join would silently drop every one of them.
    assert sql.count("LEFT OUTER JOIN") == 2


# =================================================================================
# Against a real database
# =================================================================================


def _skip_without_db() -> None:
    import socket
    from urllib.parse import urlparse

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 1):
            pass
    except OSError:
        pytest.skip(f"Postgres at {parsed.hostname}:{parsed.port} is not reachable")


@pytest.mark.integration
async def test_metrics_read_the_real_chain_end_to_end() -> None:
    """The fakes above cannot prove the SQL runs, the JSONB predicate matches, or
    that `seq` -- assigned by the database, not the application -- orders two
    dispositions written in one transaction. This does."""
    _skip_without_db()
    from comms_surveillance import audit
    from indic_platform.db.models import AnalysisRun, Call, Disposition, Flag
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    key = f"metrics-test/{uuid.uuid4()}"
    try:
        # Baseline first. `precision_by_category` is windowed by time, not by
        # call, and this database is shared with every other integration test in
        # the run -- `test_uc3_audit.py` writes its own `guaranteed_returns`
        # flag with a disposition inside the same five minutes. Asserting an
        # absolute count here is asserting that no sibling test exists, which
        # was how this first failed: `decided == 2`, one of them not ours.
        # The delta is the part this test actually established.
        async with factory() as session:
            window = now - timedelta(minutes=5)
            baseline = {
                row.category: row
                for row in await metrics.precision_by_category(session, since=window)
            }
            baseline_estimate = await metrics.false_negative_estimate(session, since=window)
        before = baseline.get(CATEGORY)
        before_decided = before.decided if before else 0
        before_false_positive = before.false_positive if before else 0

        async with factory() as session:
            call = Call(source_uri=f"s3://{key}", source_key=key, recorded_at=now)
            session.add(call)
            await session.flush()

            run = AnalysisRun(
                call_id=call.id,
                stage="deep_analysis",
                model="claude-sonnet-5",
                created_at=now,
                output={"escalation_reasons": ["qa_sample"], "escalated": True},
            )
            await audit.append(session, run)
            flag = Flag(
                call_id=call.id,
                run_id=run.id,
                category=CATEGORY,
                severity="high",
                evidence_span="x",
                created_at=now,
            )
            await audit.append(session, flag)
            # Two dispositions in one transaction: same timestamp, different seq.
            for verdict in ("confirmed", "false_positive"):
                await audit.append(
                    session,
                    Disposition(
                        flag_id=flag.id,
                        disposition=verdict,
                        reviewer_id="tester",
                        created_at=now,
                    ),
                )
            await session.commit()

        async with factory() as session:
            by_category = {
                row.category: row
                for row in await metrics.precision_by_category(session, since=window)
            }
            # Two dispositions were written against one flag. Exactly one
            # decision may appear, and it must be the later one: that is the
            # "latest disposition wins" rule, and `seq` -- assigned by the
            # database, not the application -- is what orders two rows sharing
            # a transaction timestamp.
            assert by_category[CATEGORY].decided - before_decided == 1
            assert by_category[CATEGORY].false_positive - before_false_positive == 1

            estimate = await metrics.false_negative_estimate(session, since=window)
            assert estimate.sampled - baseline_estimate.sampled == 1
            assert estimate.missed - baseline_estimate.missed == 0
    finally:
        await engine.dispose()
