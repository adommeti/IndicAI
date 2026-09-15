# ADR 0006 — Meeting minutes is a separate, separately-consented module

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `program/P0`; any future `apps/meeting_minutes` work
- **Relates to:** ADR 0005, ADR 0004 (surveillance scope, reserved), ADR 0002

## Context

`docs/prd-v2.md` E11. Multilingual meeting minutes and multilingual communications surveillance
look like the same system from an engineering angle: both are Saaras batch STT with diarization,
followed by Claude over a native-language transcript. The temptation to build one pipeline and
expose two views of it is real, and it would save a few weeks.

It is also how a productivity feature becomes a surveillance grievance. The two have different
lawful bases, different consenting parties, different retention expectations and different
audiences. A team that opts in to having its own calls summarized has not opted in to those calls
being screened for conduct findings. If the data sits in one place, the promise that it is not
being used the other way is a policy statement rather than a property of the system.

## Decision

If meeting minutes is built, it is a **separate app** — `apps/meeting_minutes` — with:

- its own **opt-in per meeting**, recorded with the meeting, not a blanket organizational consent;
- its own **storage**, with **no access to the surveillance tables** (`calls`,
  `transcript_segments`, `analysis_runs`, `flags`, `dispositions`) and no shared transcript store.
  Separation is enforced by database role and schema, not by application convention;
- its own retention rule, set by the team that owns the meetings;
- reuse of `platform/adapters` **only** — the vendor boundary from ADR 0002, and nothing else
  from UC3.

It is **not in this POC's scope** and must not be presented internally as part of it. Neither
direction of data flow is permitted: surveillance never reads minutes transcripts, and minutes
never reads surveillance output.

## Consequences

- **Positive:** the "no dual-use data path" claim is checkable — it reduces to which tables a
  role can read, which a security review can verify rather than take on trust.
- **Positive:** UC3's Gate 0 conversation (ADR 0004) stays about conduct monitoring alone, and is
  not complicated by a productivity feature riding along.
- **Positive:** minutes can be consented, piloted and cancelled independently, at a much lower
  governance cost, because nothing in the surveillance perimeter depends on it.
- **Negative:** deliberate duplication — a second ingestion path, a second STT call for the same
  meeting if a call were ever in both perimeters, a second set of storage and retention plumbing.
  That cost is accepted as the price of the separation.
- **Negative:** the shared platform package is the one thing both would touch, so a change to
  `platform/adapters` affects both. That is the intended and only coupling.
- **Follow-up required:** nothing in this repository until minutes is actually scoped. If it is,
  it needs its own PRD section, its own DB role, and a security review that demonstrates the
  table-level separation.

## Evidence

- `docs/prd-v2.md` E11 — the separate-module decision; E8 — the surveillance tables this module
  must not reach; E9 — the reviewer role model whose least-privilege design this extends.
- `apps/` — contains `helpdesk_agent`, `training_localizer`, `comms_surveillance` only. There is
  no `meeting_minutes` app and no shared transcript store; the decision is currently enforced by
  the absence of the code.
- `platform/adapters/base.py` — the only interface a future minutes app may use, per ADR 0002.
- **Not implemented, by design.** Nothing here is deferred work: the follow-up is a governance
  precondition, not an engineering task.
