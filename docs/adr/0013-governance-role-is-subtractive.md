# ADR 0013 — The `governance` role subtracts access rather than granting it

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `uc3/P6`
- **Relates to:** ADR 0011, ADR 0004 (surveillance scope, reserved), PRD E8

## Context

`uc3/P6` defines three roles on the comms surveillance app: `compliance_reviewer` works the
flag queue, `compliance_lead` is a superset that also sees the random QA sample, and
`governance` gets "metrics and chain-verification status only — no transcript or audio access
(enforce server-side)".

RBAC normally composes additively: a principal's permissions are the union of its roles'
permissions. Under that reading, a principal holding both `governance` and `compliance_reviewer`
can fetch transcripts, because the reviewer grant supplies what the governance role omits. The
prompt's acceptance criterion is that "role tests prove governance cannot fetch transcripts",
and under additive composition that sentence is only true for principals that hold
`governance` *alone*. It becomes a statement about how the directory happens to be configured
rather than a property of the system.

This matters more here than it would in most apps. The point of a governance role over a
surveillance programme is segregation of duties: the people who read the *numbers* about the
programme — precision, false-negative estimates, whether the audit chain verified — are
deliberately not the people who can read the *calls*. If one person can hold both, the control
is a UI convention, not a separation.

## Decision

`governance` is enforced as a **deny**, not as a smaller grant. `SEGREGATED_ROLES` is evaluated
*before* the per-route allow-list, so a principal carrying `governance` is refused the
transcript- and audio-bearing routes even when it also carries `compliance_reviewer` or
`compliance_lead`. The order matters for the 403 body: checking deny first means the refusal can
name the segregated role, because the code has not yet gone looking for a grant that would have
admitted the caller. (An earlier draft of this record said "after", which described neither the
code nor the behaviour it then claimed.)

A dual-hatted person therefore needs two subjects, which is what segregation of duties means.

## Consequences

**Positive.** "Governance cannot fetch transcripts" becomes unconditional — a property of the
API, provable by a test that grants a principal both roles and still expects 403. It cannot be
weakened later by a directory change that nobody reviews, and the failure mode of a
misconfigured group membership is loss of access rather than silent over-exposure.

**Negative, and worth stating plainly.**

- It violates the principle of least astonishment for anyone who expects additive RBAC. An
  operator who adds `governance` to a reviewer's existing account will see that reviewer lose
  the queue, and the API will look broken until they read this record. The 403 body names the
  segregated role for exactly this reason.
- Genuinely dual-hatted staff — a compliance lead who also sits on the governance committee —
  need two accounts, which is an operational cost borne by real people, not a free win.
- `AUTH__DEV_BYPASS` therefore cannot hand out all three roles at once: it defaults to
  reviewer + lead, and `AUTH__DEV_BYPASS_ROLES=governance` switches to the governance view. A
  bypass granting everything would deny the local UI the transcripts it exists to render, and
  a developer would reasonably read that as a bug in the API rather than as the policy working.

**Not decided here.** Whether the real Entra directory should model these as mutually exclusive
groups is a deployment question for `program/P10`; this ADR only fixes what the API does when
presented with a claim carrying both.
