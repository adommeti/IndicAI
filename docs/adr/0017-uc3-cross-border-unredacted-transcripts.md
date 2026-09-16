# 0017 — UC3 sends UNREDACTED transcripts across the border, and that is the cost of E9

- Status: accepted
- Date: 2026-09-16
- Context: uc3/P7 (security and retention pass); PRD B5, threat T4; F4 checklist item
  "Residency decision recorded in ADR; DPA/vendor risk review status noted"
- Relates to: ADR 0014 (uc1's equivalent, and deliberately not the same answer), ADR 0005,
  ADR 0016, ADR 0004 (surveillance scope, reserved)

## Context

uc3/P7's security review graded T4 **NOT MET** for a specific reason: no ADR anywhere recorded
uc3's cross-border position. uc1 has ADR 0014; uc3 had nothing, so the most sensitive
cross-border leg in the programme was the one with no decision written down. This ADR records it.

What actually leaves the process today, per leg:

| Leg | Vendor | What it receives | Redacted? | Where processed |
|---|---|---|---|---|
| Batch STT + diarization (Saaras) | Sarvam | the **raw call recording** | n/a — audio | India |
| Transliteration (Mayura, opt-in) | Sarvam | each transcript **segment** | **yes**, platform default | India |
| Triage (Haiku) | Anthropic | the **full transcript**, native script | **no** | United States |
| Deep analysis (Sonnet) | Anthropic | the **full transcript** + `policy.md` | **no** | United States |
| English rendering (Haiku) | Anthropic | one **evidence span** | **no** | United States |

PRD B5's option (b) for UC3 is "Claude sees text transcripts (never audio) **with configurable
redaction**". uc3 implements the first half and deliberately switches off the second: PRD E9
requires it, because an off-channel-comms finding can turn on the phone number itself and a
reviewer handed `[PHONE]` as the evidence cannot act on it.

So the payload crossing the border is the **unredacted verbatim transcript of a recorded employee
call** — which B5 itself classifies Confidential / regulated. That is a materially different
exposure from uc1's, where the same border is crossed by redacted text (ADR 0014), and the two
must not be read as one decision applied twice.

There is a second asymmetry worth naming, because it looks like an inconsistency and is one:
`ingest.romanise` builds a **default** `SarvamTranslate()`, which *does* redact. So when
`SARVAM_TRANSLITERATE=true`, the Roman copy that stays in India is masked while the native copy
sent to the United States is not. The E9 override is applied per-adapter rather than per-app, and
it currently lands on exactly the wrong side of the border.

## Decision

**Accept the cross-border Claude leg for the synthetic POC only, and make the acceptance expire
at the same gate that governs everything else about real data.**

1. **Audio never crosses the border.** Structural, not a promise: `platform/adapters/claude.py`
   exposes `structured` and `stream_text`, both text-only, with no audio parameter on either. A
   capability that would send a recording to a US-hosted model is a new decision, not an
   extension of this one.
2. **The transcript leg is unredacted, by the documented E9 override**, and that override is
   recorded in `apps/comms_surveillance/README.md` and pinned by a test. It is a deliberate
   trade of confidentiality for evidentiary usefulness, not an oversight.
3. **This ADR authorises synthetic data only.** Everything uc3 has ever processed is the
   synthetic golden set. The moment a real recording is in scope, this acceptance lapses and
   requires: ADR 0004 (scope and lawful basis), a signed DPA with Anthropic covering employee
   communications content, and a completed vendor risk review. None of the three exists today.
4. **The transliteration asymmetry is a defect, recorded not fixed here.** The correct end state
   is one redaction policy chosen per application and applied to every vendor leg it has. Fixing
   it inside this prompt would change what Stage 0 matches on, which needs an eval run against
   the lexicon gate rather than a config change at the end of a security pass.

## Consequences

**Positive.** The decision is now written down and reviewable, which is what F4 line 4 asks for.
T4 moves from NOT MET (no record at all) to **PARTIAL**: the residency decision is recorded and
its conditions are explicit; the DPA and vendor risk review are named as outstanding rather than
assumed. A reader can see exactly which leg carries what, and that uc3's answer differs from
uc1's and why.

**Negative, stated plainly.** Recording a decision does not reduce the exposure. Until the DPA
exists, the honest description of uc3 is: a pipeline that would send unredacted verbatim
employee call transcripts to a US-hosted model, running today only because the transcripts are
invented. Anyone reading this ADR as clearance to point uc3 at a real recording has read it
backwards — clause 3 is the operative one.

**On the gap between this and ADR 0014.** uc1 crosses the border with redacted text; uc3 crosses
it with unredacted text. Both are defensible for what each app does, and neither generalises to
the other. A future reviewer looking for "the programme's residency decision" will not find one:
there are two, they differ, and the difference is the E9 override.

## Alternatives considered

- **Redact the Claude leg like uc1 does** — rejected by PRD E9, not by us: it defeats the
  detection task the app exists to perform.
- **Route uc3's analysis to an India-hosted model** — the honest alternative, and out of scope
  here: it is a change of vendor for the detection stages, with its own eval run, and belongs to
  the B5 conversation rather than to a security pass. Recorded so the option is not lost.
- **Redact only the identifiers no category needs as evidence** — the most promising middle
  ground (mask government IDs and account numbers; preserve phone and email, which
  `off_channel_comms` and `personal_trading` genuinely need). Needs a per-category evidence
  analysis and a lexicon eval run to show recall is unharmed. Worth a prompt of its own.
- **Say nothing until Compliance decides** — what the repository did until now, and the reason
  the review graded T4 NOT MET. An unrecorded decision is still a decision; it is just one
  nobody can review.
