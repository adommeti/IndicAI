# ADR 0011 — The UC3 audit chain is tamper-evident, not non-repudiable

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `uc3/P5`; notarisation owned by `program/P10-azure-deploy`
- **Relates to:** ADR 0010, ADR 0012, ADR 0005, ADR 0004 (surveillance scope, reserved)

## Context

`docs/prd-v2.md` E8 specifies a hash chain over `analysis_runs`, `flags` and `dispositions`, a
DB role with `INSERT`/`SELECT` only, and a nightly job that re-walks the chains. Those three
things are now built. The words "hash chain" invite a stronger reading than the construction
supports, and this app's output is conduct findings about named employees — a disputed flag or a
disputed disposition is exactly where someone will ask what the chain proves. This ADR fixes the
claim that may be made, so that neither the README, the reviewer UI, nor a pilot conversation
with Compliance overstates it.

The construction is an **unkeyed** sha256 chain stored in the same database as the rows it
protects. No secret is involved in computing a hash, so no secret is needed to recompute one.

## Decision

We describe this control as **tamper-evident against an adversary who does not hold write
privilege on the audit tables**, and we do not claim non-repudiation anywhere.

**What it defeats.**

- The application itself. `uc3_app` has `SELECT, INSERT` only on the three tables, so an
  application bug, a SQL injection, or a stolen application credential cannot edit or delete
  history at all — only append, which the chain then binds. Tampering requires a *second*,
  higher-privileged credential.
- Accidental corruption, partial restores, and bugs in the append path: all present as breaks.
- A single-row edit or delete by a privileged actor who does not re-chain. `chain_verify` names
  the first bad row and whether it was edited or removed, within one night.

**What it does not defeat.** An actor with `UPDATE` on these tables — superuser, the table
owner, anyone who can restore a snapshot or reach the data files — can change any row and then
recompute every subsequent `row_hash`. Nothing stops them: the hash needs only the rows
themselves. The cost is the same O(n) walk the verifier performs, and the implementation's own
benchmark measures that walk at **10,003 rows in 0.46 s**. Re-chaining a year of this app's
output is a few seconds of work. Afterwards `chain_verify` reports `ok`, because the forged
chain is internally consistent. The chain raises the cost of tampering from one UPDATE to one
UPDATE plus a script; it does not make tampering infeasible.

Two further limits, stated so they are not discovered later:

- **The local anchor** (head hash plus row count per table, `audit_chain_anchors`, added by
  `0009_uc3_chain_anchor` and moved only after a clean verification) closes one hole the walk
  cannot see at all: a truncated tail leaves a prefix that is internally consistent and verifies
  clean, so `delete from analysis_runs where seq > 400` passes silently without it. It also
  raises the bar on an edit to "rewrite the tail *and* the anchor row". It is not a trust root:
  it lives in the same database, under the same privilege, and is recomputed with the same
  unkeyed hash.
- **The least-privilege role is a property of the deployment, not of this repository.** If the
  login role the application uses also owns the tables, it carries `UPDATE`/`DELETE` implicitly
  and can re-grant them to itself, and the control is worth nothing whatever the migration says
  (see the deployment precondition in `0008_uc3_audit_role.py`).

**What non-repudiation would require**, and what is therefore deferred: an anchor the tamperer
cannot recompute. Concretely, a daily `(table, head_hash, row_count, timestamp)` record either
signed with a key the database hosts cannot reach (Key Vault, per `program/P10-azure-deploy`),
written to WORM/immutable object storage with a retention lock, or notarised externally —
combined with PITR backups held under a different credential from the production database.
Verification then compares the walked head against an artefact outside the blast radius, and a
mismatch cannot be papered over from inside the database.

**None of that is implemented in `uc3/P5`.** It is deployment scope, owned by
`program/P10-azure-deploy`, and recorded in `docs/build/BLOCKERS.md`.

## Consequences

- **Positive:** the claim is checkable. A security reviewer can verify "tampering needs a second
  credential and an edit that is not re-chained is detected within 24 h" from the grants, the
  tests and the beat schedule, and needs to take nothing on trust.
- **Positive:** the detection window is bounded and monitored — `uc3_audit_chain_breaks` is a
  gauge an on-call rota can alert on, not a log line someone has to read.
- **Negative:** the control is weakest exactly where the risk is highest. A surveillance system
  whose findings concern employees may one day produce a finding about someone with, or close
  to, database administration rights. That is a realistic insider threat for this app
  specifically, and until the anchoring work lands it is mitigated by who holds superuser —
  an operational control, not a technical one.
- **Negative:** until then, an auditor asking "prove this disposition was not written after the
  fact" cannot be answered by the system. The honest answer is that the row is consistent with
  every row after it and that no break has been observed, which is weaker than it sounds.
- **Negative:** one grant is still outstanding. `audit_chain_anchors` is closed to `uc3_app`
  only because Postgres 16 grants nothing to `PUBLIC` on a new schema object; the explicit
  `revoke all on table audit_chain_anchors from uc3_app` belongs beside the other revokes in
  `0008_uc3_audit_role.py`, which is shipped and is another change's to amend. Until it is there,
  the anchor's protection rests on a default rather than on a statement.
- **Follow-up required:** `program/P10-azure-deploy` — signed or WORM-anchored daily heads, and
  backup credentials separated from the production role. A later revision must add the explicit
  anchor revoke to the role's grant list. `uc3/P7` maps this control to the
  threat table and must carry the limitation, not the stronger claim.

## Evidence

- `apps/comms_surveillance/audit.py` — the module docstring states the same bounded claim at
  the point of use ("what this actually defeats, and what it does not"); `verify_chain`
  distinguishes edit from insert/delete/reorder and compares the anchor; `ChainResult.anchor_ok`
  is `None`, not `True`, when no anchor was compared, so an un-anchored chain cannot read as a
  stronger result than it is; `run_chain_verify` and `emit_chain_metric` publish
  `uc3_audit_chain_breaks` as metadata only.
- `apps/comms_surveillance/ingest.py` — `uc3.chain_verify` beat entry, 05:00 daily: the
  detection window is one night, not continuous.
- `platform/db/migrations/versions/0008_uc3_audit_role.py` — `revoke all` then
  `grant select, insert` on the three tables, the `revoke update, delete ... from public`, and
  the deployment precondition about table ownership.
- `platform/tests/test_uc3_audit.py::test_the_app_role_cannot_update_or_delete_the_audit_tables`
  — a `ProgrammingError` per statement; Postgres logged four denials in the same CI run.
  `::test_a_superuser_edit_is_detected_by_chain_verify` proves precisely the bounded claim: a
  superuser edit *that does not re-chain* is detected. No test asserts anything about a
  re-chained forgery, because no test could — the forgery verifies.
- Measurement: `chain_verify: 10003 rows in 0.46s` (CI run 55). Read as the defender's budget it
  is 20× under the 10 s acceptance threshold; read as the attacker's budget it is the reason
  this ADR exists.
- **Not implemented — owned by `program/P10-azure-deploy`:** signed or externally notarised
  daily head hashes, WORM storage for anchors, separated backup credentials. Recorded in
  `docs/build/BLOCKERS.md`.
