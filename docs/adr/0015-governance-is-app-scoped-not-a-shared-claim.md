# ADR 0015 — `governance` is an app-scoped role name, and uc1's is not uc3's

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `uc1/P6`
- **Relates to:** ADR 0013 (governance is subtractive, uc3), PRD C8, PRD E8

## Context

Two prompts in this programme use the word `governance`, and they mean opposite things.

**uc3** (ADR 0013): `governance` is enforced as a **deny**. A principal carrying it is refused
transcript- and audio-bearing routes *even if it also carries a content role*. It is oversight
without content access, and the whole point is segregation of duties over a surveillance
programme — the people who read the numbers are deliberately not the people who can read the
calls.

**uc1** (this prompt): `GET /sessions/{id}/replay` is "restricted to a `governance` role". Replay
is the most content-dense response the helpdesk app has — every utterance, every decision, every
retrieval, for a whole session, in one payload. Here `governance` is the role that **grants** the
most content access in the app.

So the same claim name is a deny in one app and the broadest grant in the other. That is not a
naming quibble. Roles arrive from Entra ID as group claims, and a directory administrator adding
someone to a group called `governance` because they need helpdesk oversight would, if the same
group were mapped to uc3, simultaneously *remove* that person's ability to read surveillance
transcripts — or, read the other way round, a person placed in `governance` for uc3 segregation
would silently gain full helpdesk replay. Either direction is a surprise, and neither shows up in
a code review of one app, because each app's code is locally correct.

The PRD is read-only and says `governance` for uc1 replay, so renaming the requirement away is
not available.

## Decision

Implement the PRD's requirement — uc1 replay is gated on `governance` — and treat the role as
**app-scoped**, stated explicitly in code rather than left for a deployment to discover.

1. uc1 gets its own `apps/helpdesk_agent/roles.py`. It does **not** import
   `apps/comms_surveillance/auth.py` and does not reuse its constants, so the two cannot drift
   into each other by refactor. A test pins that uc1's replay check does not consult uc3's role
   sets.
2. Each module's docstring names the other and says the meaning is inverted, so whichever file a
   reader opens first, they learn there is a second one.
3. **Deployment requirement, and the operative half of this record:** the two apps MUST be mapped
   to *different* Entra groups. Something like `uc1-helpdesk-governance` and
   `uc3-surveillance-governance`. Mapping one group to both is a misconfiguration that grants and
   denies at the same time, and no amount of care inside either codebase detects it, because from
   each app's side the claim is simply present.

## Consequences

- Neither app has to bend its own security model to the other's. uc3 keeps its subtractive
  guarantee, which ADR 0013 argues for at length and which a shared additive role would destroy.
- The cost is a name that means two things, mitigated by documentation rather than eliminated.
  That is a real residual risk and it lives in the directory, not in this repository — so it
  belongs in the deployment checklist (`program/P10`, `program/P11`) as well as here, and is
  recorded in `docs/security/uc1-review.md` under T3.
- If a later prompt is free to choose the name, the better fix is to drop `governance` in uc1 for
  something that describes what it grants — `replay_auditor`, say. This ADR is the cheaper
  decision available while the PRD's text is fixed, not the one I would choose on a blank page.
- A third app reusing either name inherits this problem. The general rule this sets: role names in
  this programme are scoped to their app unless an ADR says a claim is shared, and none does.
