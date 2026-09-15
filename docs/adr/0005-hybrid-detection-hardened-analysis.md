# ADR 0005 — Hybrid lexicon + LLM detection with a hardened analysis harness

- **Status:** Accepted (design); pipeline not yet built
- **Date:** 2026-09-15 · **Owner:** `uc3/P3` (lexicon), `uc3/P4` (triage, deep analysis, verifier)
- **Relates to:** ADR 0004 (surveillance scope, reserved), ADR 0006, ADR 0001

## Context

`docs/prd-v2.md` E5 and E9. Communications surveillance is the one app where being wrong is
expensive in both directions: a missed disclosure is a regulatory failure, a false flag is a
person's employment record. Two properties matter more than raw accuracy.

First, **explainability to an auditor.** A pure-LLM detector has no floor an auditor can read.
Second, **resistance to the transcript itself**: the input is speech by the people being
monitored, and anyone who knows they are monitored can address the analyser directly. Treating a
transcript as a prompt is the whole attack surface. Translating everything to English first —
v1's design — makes both worse: ~2.7M characters/month of Mayura, and a paraphrase layer between
the risk language and the reviewer.

## Decision

**Three stages, hybrid by design.**

- **Stage 0 — lexicon (deterministic).** Versioned YAML per category, native-script and
  Roman-transliteration variants, weights and severity, run on the native transcript plus a
  Sarvam-transliterated Roman copy. The recall floor, and the artefact an auditor reads.
- **Stage 1 — Haiku triage on every call.** Cached system prompt, returns
  `{risk_score, candidate_categories}`. It scores; it decides nothing.
- **Stage 2 — Sonnet deep analysis, escalated calls only.** Escalate on any high-severity
  lexicon hit, `risk_score ≥ θ` (start θ=60), **or** membership in a random 5% QA sample — the
  sample is how false negatives on the clean stream get estimated rather than assumed zero.

**Analysis in the native language.** Per ADR 0001, evidence spans are kept verbatim; an English
rendering is produced only for flagged spans.

**Hardened harness — all four properties required, not optional.** Claude is never the
decision-maker here: every output is a candidate finding for human review.

1. **Tool-less.** Stage 1 and Stage 2 calls have no tools. JSON output only. No exfiltration
   channel exists to be abused.
2. **Untrusted by construction.** The transcript is wrapped and labelled as data in both prompts;
   `instruction_like_content` is itself a flag category, so an attempt to talk the system out of
   flagging is escalated to a human rather than silently obeyed.
3. **Verified.** A deterministic verifier drops, and counts, any flag whose `evidence_span` is not
   an exact substring of the transcript and any output failing schema validation.
4. **Canaried.** A per-deployment secret phrase sits in the system prompt; any output containing it
   is treated as compromised, discarded and alerted.

## Consequences

- **Positive:** recall has a deterministic floor that survives a model change, and precision has a
  human gate. Every flag carries `policy_version`, `lexicon_version`, `prompt_version`, `model` and
  `θ`, so a disputed finding is *auditable* — not bit-reproducible, since Sonnet 5 takes no
  temperature control (see Evidence); Stage 0 is the part that replays exactly.
- **Positive:** injection resistance is *testable*, not asserted — 20 adversarial golden
  transcripts, build-blocking at a 0% success gate. Cost scales with risk: Haiku on everything,
  Sonnet on ~10%.
- **Negative:** the lexicon is a maintenance burden owned by Compliance and the component most
  likely to rot; its and `policy.md`'s changes go through PRs that re-run the UC3 evals. θ and the
  5% sample rate are guesses until pilot data tunes them.
- **Negative:** verbatim evidence requires transcripts *not* to be redacted for phone/email — an
  override of the program-wide redaction rule that raises the access-control stakes on the review
  queue. Not implemented: `apps/comms_surveillance/README.md:4` still reads "No redaction override
  is enabled"; `uc3/P4` writes the documented override.
- **Follow-up required:** ADR 0004 (scope, lawful basis, retention) gates any contact with real data.

## Evidence

- `platform/security/harden.py:12` `wrap_untrusted` — data labelling and escaping; `:19`
  `new_canary`; `:23` `validate_or_reject` rejects on canary leak before schema validation; `:31`
  `evidence_is_exact` — the verifier's substring rule.
- `platform/tests/test_controls.py:21` `test_hardening` — delimiter escaping, evidence exactness
  and canary uniqueness; `platform/tests/test_eval.py:23` `test_canary_rejected` — the rejection.
- `platform/adapters/claude.py:24` `structured` — cached system prompt, schema-constrained output,
  no tool parameter ever passed. Note `:44`: `temperature: 0` is sent to Haiku but **not** to
  Sonnet 5, which rejects legacy sampling controls, so Stage 2 is not sampling-deterministic.
- `docs/prd-v2.md` E5–E10 — stages, prompt text, audit chain, golden set, gates.
- **Not implemented:** the adversarial golden set (`uc3/P1`); lexicon YAMLs (`uc3/P3`); triage and
  deep-analysis prompts and the verifier (`uc3/P4`); the hash-chained audit tables (`uc3/P5`).
