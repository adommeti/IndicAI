# 0016 — Retention deletes call content, not the audit chain

- **Status:** Accepted
- **Date:** 2026-09-16
- **Prompt:** `uc3/P7`
- **Relates to:** ADR 0010, ADR 0011, ADR 0013, ADR 0004 (surveillance scope, reserved), PRD T5, T7, E8

## Context

PRD T5 requires retention "per Compliance's recordkeeping rule", enforced by a scheduled
job that logs its deletions. PRD T7 requires the audit trail to be append-only and
hash-chained, with the application's database role holding INSERT but not UPDATE or
DELETE on those tables. uc3/P5 implemented T7; uc3/P7 implements T5.

The two requirements collide, and the collision is structural rather than a matter of
implementation order. Three facts, each verified against a live PostgreSQL 16 rather
than read off the models:

1. `analysis_runs.call_id` and `flags.call_id` are foreign keys onto `calls.id`, with
   `NO ACTION` on delete.
2. The `uc3_app` role holds `INSERT, SELECT` on `analysis_runs`, `flags` and
   `dispositions`, and `SELECT, INSERT, UPDATE, DELETE` on `calls` and
   `transcript_segments` (migration `0008_uc3_audit_role`, which also revokes
   `UPDATE, DELETE` from `PUBLIC` on the chained tables).
3. `flags.evidence_span`, `flags.english_rendering`, `flags.reasoning` and
   `analysis_runs.output` all hold verbatim call content.

Running the deletions as the application's own role, inside one transaction:

```
delete from transcript_segments where call_id = ...  ->  DELETE 1
delete from calls where id = ...                     ->  ERROR:  update or delete on table
                                                         "calls" violates foreign key
                                                         constraint "analysis_runs_call_id_fkey"
                                                         on table "analysis_runs"
delete from analysis_runs where id = ...             ->  ERROR:  permission denied for table
                                                         analysis_runs
```

So a call that has been analysed cannot have its `calls` row deleted, and the rows
blocking that deletion cannot be cleared out of the way. This is the chain working as
designed — ADR 0011 calls the chain tamper-evident, and a retention job that could
delete chained rows would be precisely the privilege a tamperer needs. It is not a bug
to be fixed by widening the grant.

## Decision

uc3's retention sweep deletes **call content** and does not touch the chain:

- **Deleted:** `transcript_segments` rows (`text` and `text_roman`, the authoritative
  record of what was said), and the source recordings in object storage.
- **Not deleted:** `calls`, `analysis_runs`, `flags`, `dispositions`.

`calls` is not a policy target. Including it would produce a job that fails on a foreign
key every night for every analysed call, which is worse than one that states its limit;
and the row is metadata (object key, duration, speaker labels, cost) rather than
content.

We do **not** widen the `uc3_app` grant, add `ON DELETE CASCADE`, or re-anchor the chain
from the retention job. Each of those converts a scheduled job into something that can
erase the audit trail, which is the one thing T7 exists to prevent.

## Consequences

**Positive.** The bulk of the sensitive content — every verbatim transcript and all
audio — is inside the retention window. The chain stays verifiable indefinitely, because
nothing the job does removes a row it walks. The sweep never fails on a foreign key, so
a red retention job means a real fault rather than the design.

**Negative, and this is the part a reviewer must not have to derive.** Evidence spans
quoted into a flag are **outside** any retention window this application can enforce.
A call whose transcript has been deleted still has its flagged lines quoted verbatim in
`flags.evidence_span`, and its full Stage 2 output in `analysis_runs.output`, for as long
as the chain exists. Concretely:

- An erasure request cannot be satisfied for quoted evidence by anything uc3 runs. It
  needs a privileged operator, outside the application, and a chain re-anchor
  afterwards — otherwise every subsequent verification reports the gap as tampering.
- "uc3 has a 90-day retention policy" would therefore be a false statement about flagged
  calls, whatever number replaces 90. The accurate statement is: transcripts and
  recordings are deleted on the configured schedule; quoted evidence in the audit trail
  is retained for the life of the trail.
- Whether that is acceptable is a lawful-basis question, not an engineering one, and it
  belongs in the **ADR 0004** conversation with Compliance, Legal and HR — alongside the
  window itself. It is listed here so that conversation starts with the constraint
  already on the table rather than discovering it during the first erasure request.

**Bounded, like ADR 0011.** That ADR bounds what the chain proves (tamper-evident, not
non-repudiable). This one bounds what retention reaches. Both exist because the PRD
states a control more confidently than the implementation can earn, and the honest move
is to write down the gap rather than to let the control's name imply it.

## Alternatives considered

- **Redact in place instead of deleting** — overwrite `evidence_span` with a tombstone.
  Rejected: it is an UPDATE, which the role cannot perform and which the chain treats as
  tampering by construction. Granting it would defeat T7 for the sake of T5.
- **`ON DELETE CASCADE` from `calls`** — rejected for the same reason in a different
  costume: the cascade deletes chained rows without the role ever needing DELETE on
  them, so the audit trail becomes erasable by deleting a call.
- **Keep evidence out of the chain and hash a pointer instead** — genuinely better, and
  not available now: it changes what the chain attests to, needs a `HASH_SCHEMA_VERSION`
  bump that invalidates every stored hash, and would leave the evidence in a table with
  its own integrity problem. Worth reconsidering if the chain is ever re-based; recorded
  here so the option is not lost.
- **Do nothing until ADR 0004 exists** — rejected. The job ships disabled and rehearsing,
  which proves the schedule is deployed and shows what the first real pass would remove.
  A retention policy that has never run looks identical, in the database, to one that ran
  and found nothing.
