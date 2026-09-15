# ADR 0010 — The audit chain walks a `seq` identity column, and `created_at` is application-set

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `uc3/P5`
- **Relates to:** ADR 0005, ADR 0011, ADR 0012, ADR 0004 (surveillance scope, reserved)

## Context

`docs/prd-v2.md` E8 gives the three chained tables as SQL and defines
`row_hash = sha256(prev_hash || canonical_json(row_without_hashes))`, with `prev_hash` being
"the previous row's hash in insertion order per table". That SQL does not materialise insertion
order. Its only candidate keys are `id uuid` (random) and `created_at timestamptz default now()`
— and `now()` is the transaction timestamp, so every row written in one transaction carries the
same value. Ordering by `created_at` leaves ties broken by whatever the planner returns, which
means the verifier can walk rows in an order different from the one the appender hashed against
and report a break in a chain nobody touched.

The second problem is the same problem one step later. A column default assigned by the server
— `created_at default now()`, and on the Python side `default=uuid.uuid4` — is applied when the
INSERT is emitted, which is *after* the application has computed `row_hash`. The row that was
hashed and the row that was stored are then different rows. This is not hypothetical: the first
implementation hashed rows with `id=None`, and every chain broke at its first row with
"row_hash does not match the row content" — which is exactly what a real edit looks like. A
tamper-evidence control that cries wolf is worse than none, because it teaches its readers to
close the alert.

## Decision

Two additions to E8's schema. Neither changes what is stored or what the hash formula is.

1. **Each chained table carries `seq`** — a unique `bigint` identity column, `GENERATED
   ALWAYS` — and `seq` is the chain's order. `append` takes the head as the `row_hash` of the
   highest `seq`; `verify_chain` walks in `seq` order by keyset (`seq > last`), which only a
   monotonic key makes possible. `seq` can never be part of the hashed payload — the database
   assigns it after the application has hashed, so hashing it would be unsatisfiable, and
   `_validate_hashed_columns` refuses it at import along with the two hash columns. It is still
   bound to the chain indirectly: it defines the walk order, and an insert, delete or reorder
   shows up as a `prev_hash` mismatch at the first affected row.

   `GENERATED ALWAYS`, not `BY DEFAULT`, and the difference is the whole control: `BY DEFAULT`
   lets the client choose `seq`, and INSERT is exactly the privilege `uc3_app` holds. One row
   appended with a very large `seq` sorts after every append that follows it, so each later
   row's `prev_hash` points at the wrong predecessor and the chain breaks for all of them. It is
   unrepairable by the app, because the same role has no UPDATE or DELETE — the least privileged
   credential in the system could destroy the audit trail's integrity and only a superuser could
   put it back. `GENERATED ALWAYS` makes the database refuse a client-supplied value.

2. **`created_at` is `NOT NULL` with no server default.** The application sets it, in UTC,
   before hashing, together with every other Python-side default
   (`audit.materialise_defaults`). The rule generalises: nothing that the hash covers may be
   assigned by the database. The value is also normalised to UTC again when it is rendered for
   hashing, because psycopg returns a `timestamptz` in the reading session's `TimeZone` — hashing
   the offset would make a connection set to `Asia/Kolkata` report the whole history as tampered.

Appends additionally take a per-table transaction-scoped advisory lock
(`pg_advisory_xact_lock`), because computing `prev_hash` is a read-modify-write and two
concurrent appenders otherwise read the same head and write a fork — a break caused by nothing
but concurrency.

This ADR covers *ordering and hashing inputs only*. What the resulting chain does and does not
defeat is ADR 0011.

## Consequences

- **Positive:** the chain has a total order the database maintains, so `append` has a
  deterministic head and `verify_chain` a deterministic walk. The two failure modes stay
  distinguishable — `prev_hash` mismatch means inserted/deleted/reordered, `row_hash` mismatch
  means edited — which is the difference between "a row went missing" and "a row was changed".
- **Positive:** the hash covers the row that is actually written, so a break is evidence about
  the data rather than about the ORM.
- **Negative:** an identity sequence has gaps. A rolled-back append burns a number, so a gap in
  `seq` is *not* evidence of a deletion and nothing may alert on one; only the `prev_hash`
  linkage is evidence. Verification checks order, never arithmetic on `seq`.
- **Negative:** `created_at` is now the application's clock, not the database's. It is data
  inside the hash, not an independent time source, and clock skew between app hosts can make it
  non-monotonic with respect to `seq`. Anything that needs a trustworthy time — an
  externally-anchored daily head, for one — cannot take it from this column.
- **Negative:** a deviation from E8's literal SQL, which is why this record exists. The
  migration and the module docstrings state it at the point of use as well.
- **Follow-up required:** none in this prompt. The anchoring work that would need a trusted
  timestamp is `program/P10-azure-deploy` (ADR 0011).

## Evidence

- `apps/comms_surveillance/audit.py` — `NOT_HASHED` (the three unhashed columns),
  `canonical_json`, `row_hash`, `head`, `append` (advisory lock, then hash, then insert),
  `materialise_defaults`, `HASHED_COLUMNS` with `_validate_hashed_columns` (the payload is a
  pinned include-list, so `seq` and the hashes cannot drift into it), `verify_chain` (keyset walk
  over `seq`).
- `platform/db/models.py` — `AnalysisRun`, `Flag`, `Disposition`: `seq` identity, `created_at`
  with no `server_default` (unlike every other table in the file).
- `platform/db/migrations/versions/0007_uc3_audit_chain.py` — `_chain_columns()`; its docstring
  records the same two deviations. `0010_uc3_seq_generated_always.py` — the `set generated
  always` hardening and why it could not be an edit to 0007.
- `platform/tests/test_uc3_audit.py::test_column_defaults_are_applied_before_the_row_is_hashed`
  and `::test_hashing_the_same_row_twice_agrees_after_defaults_are_applied` — the ORM-default
  bug cannot return. `::test_the_hashes_and_seq_are_not_part_of_what_is_hashed` — the payload
  boundary. `::test_a_deleted_row_is_detected_as_a_break` — order is load-bearing.
  `::test_concurrent_appends_produce_a_chain_not_a_fork` — the lock.
- Measurement: `chain_verify: 10003 rows in 0.46s` (CI run 55, integration job), against the
  prompt's 10 s threshold for 10k rows.
