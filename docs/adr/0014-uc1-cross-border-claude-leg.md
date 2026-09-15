# 0014 — UC1 sends redacted text across the border to Claude, audio never leaves India

- Status: accepted
- Date: 2026-09-15
- Context: uc1/P7 (security and retention pass); PRD B5, threat T4; F4 checklist item
  "Residency decision recorded in ADR; DPA/vendor risk review status noted"

## Context

The helpdesk voice path crosses two vendors with different residency stories, and PRD B5 names
the decision but does not make it. F4 requires it to be recorded before the security pass can be
called green, so this ADR makes it and states what it is conditional on.

What actually leaves the process today, per leg:

| Leg | Vendor | What it receives | Where it is processed |
|---|---|---|---|
| STT (Saaras) | Sarvam | the employee's **raw audio**, PCM16, unredacted | India |
| TTS (Bulbul) | Sarvam | the reply **text**, redacted by `platform/adapters/sarvam_tts.py` | India |
| Decision (Sonnet) | Anthropic | the **transcript text**, redacted by `platform/adapters/claude.py` | United States by default |

The asymmetry matters more than it first looks. Redaction is a text hook — its own docstring says
audio is not transcribed by it — so the leg that carries the *least* processed, most identifying
data (the actual recording of someone's voice) is also the leg it cannot protect. That is
tolerable only because that leg stays in-country.

## Decision

**Option (a) from PRD B5, narrowed: accept the cross-border Claude leg for the POC, on the
condition that it carries redacted text only and never audio.**

Concretely, and enforced rather than intended:

1. **Audio never crosses the border.** No audio reaches Anthropic on any path. The voice pipeline
   streams PCM only to Saaras; the Claude leg is handed the transcript. A future capability that
   would send audio to a US-hosted model is a new decision, not an extension of this one.
2. **The Claude leg is redacted by default.** `Claude.redact` defaults to the full
   `platform/security/redact.redact` hook. UC3's documented exemption (PRD E9, which needs phone
   and email preserved as evidence) is passed explicitly as a policy object; UC1 passes nothing
   and therefore gets the default. `apps/helpdesk_agent/README.md` documents no override, which
   under CLAUDE.md is what makes the default binding for this app.
3. **Voice-path minimisation goes further than the chat path.** The transcript is redacted before
   it reaches `decide` at all, not only at the adapter boundary, so the reduction applies to
   anything downstream of the pipeline rather than to the vendor call alone.
4. **The pilot population is India-based**, which is what makes the Sarvam leg the in-country one.
   A pilot population outside India changes the premise of this ADR and reopens it.

## Status of the gate this decision depends on

PRD B5 says a "DPA + vendor risk review is a gate before real data". **That gate has not been
passed.** No DPA with either vendor has been executed in this repository's scope, and no vendor
risk review has been recorded. This ADR therefore authorises the architecture for **synthetic and
consented pilot data only**, which is what the golden set and the pilot are. It does not authorise
processing real employee calls, and nothing in the build should be read as claiming it does.

Two other things stand between this and real data, both tracked in `docs/build/BLOCKERS.md`:
the recorded consent notice does not exist (so the voice channel refuses to start at all), and
until uc1/P7's retention job runs in a deployment, nothing deletes a transcript.

## Consequences

- The residency story is now a property of the code, not a promise: audio-to-Anthropic is absent
  by construction, and redaction on the Claude leg is the default rather than an opt-in.
- If an in-region requirement is later confirmed, option (c) — Claude via an India region through
  a cloud marketplace — is the escape hatch, and it is a configuration change at the adapter
  rather than a redesign, because nothing above the adapter knows where the model is hosted.
  PRD B5 is explicit that current availability must be checked with Anthropic or the cloud
  provider rather than assumed, and this ADR does not assume it.
- The cost of choosing (a) is that a transcript — redacted, but still the substance of what an
  employee said to their IT helpdesk — is processed in the United States. That is the residual
  risk this decision accepts, and it is the thing to put in front of Legal rather than the
  architecture diagram.
