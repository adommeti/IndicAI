# ADR 0005 — Hybrid lexicon + LLM detection with a hardened analysis harness

- **Status:** Accepted (design); pipeline not yet built
- **Date:** 2026-09-15
- **Owner:** `uc3/P3` (lexicon), `uc3/P4` (triage, deep analysis, verifier, canary)
- **Relates to:** ADR 0004 (surveillance scope, reserved), ADR 0006, ADR 0001

## Context

`docs/prd-v2.md` E5 and E9. Communications surveillance is the one app where being wrong is
expensive in both directions: a missed disclosure is a regulatory failure, and a false flag is
a person's employment record. Two properties matter more than raw accuracy.

First, **explainability to an auditor.** A pure-LLM detector has no floor an auditor can read.
Second, **resistance to the transcript itself**: the input is speech by the people being
monitored, and anyone who knows they are monitored can address the analyser directly. Treating a
transcript as a prompt is the whole attack surface.

Translating everything to English first — v1's design — makes both worse: ~2.7M characters/month
of Mayura, and a paraphrase layer between the risk language and the reviewer.

## Decision

**Three stages, hybrid by design.**

- **Stage 0 — lexicon (deterministic).** Versioned YAML per category, native-script and
  Roman-transliteration variants, weights and severity. Runs on the native transcript plus a
  Sarvam-transliterated Roman copy. This is the recall floor and the artefact an auditor reads.
- **Stage 1 — Haiku triage on every call.** Cached system prompt, returns
  `{risk_score, candidate_categories}`. It scores; it decides nothing.
- **Stage 2 — Sonnet deep analysis, escalated calls only.** Escalate on any high-severity
  lexicon hit, `risk_score ≥ θ` (start θ=60), **or** membership in a random 5% QA sample — the
  sample is how false negatives on the clean stream get estimated rather than assumed zero.

**Analysis in the native language.** Per ADR 0001, evidence spans are kept verbatim; an English
rendering is produced only for flagged spans.

**Hardened harness — all four properties are required, not optional.**

1. **Tool-less.** Stage 1 and Stage 2 calls have no tools. JSON output only. No exfiltration
   channel exists to be abused.
2. **Untrusted by construction.** The transcript is wrapped and labelled as data in both prompts;
   `instruction_like_content` is itself a flag category, so an attempt to talk the system out of
   flagging is escalated to a human rather than silently obeyed.
3. **Verified.** A deterministic verifier drops any flag whose `evidence_span` is not an exact
   substring of the transcript, any output failing schema validation, and counts the drops.
4. **Canaried.** A per-deployment secret phrase sits in the system prompt; any output containing
   it is treated as compromised, discarded and alerted.

Claude is never the decision-maker: the output is a candidate finding for human review.

## Consequences

- **Positive:** recall has a deterministic floor that survives a model change, and precision has
  a human gate. Every flag carries `policy_version`, `lexicon_version`, `prompt_version`, `model`
  and `θ`, so a disputed finding is reproducible.
- **Positive:** injection resistance is *testable* rather than asserted — 20 adversarial golden
  transcripts with a build-blocking 0% success gate.
- **Positive:** cost scales with risk, not volume: Haiku on everything, Sonnet on ~10%.
- **Negative:** the lexicon is a maintenance burden owned by Compliance, and it is the component
  most likely to rot. Lexicon and `policy.md` changes go through PRs that re-run the UC3 evals.
- **Negative:** θ and the 5% sample rate are tuned on pilot data; until then both are guesses.
- **Negative:** verbatim evidence means transcripts are *not* redacted for phone/email. That is a
  deliberate override of the program-wide redaction rule, documented in the app README, and it
  raises the access-control stakes on the review queue.
- **Follow-up required:** none of the pipeline exists yet. `apps/comms_surveillance/` is an API
  skeleton. ADR 0004 (scope, lawful basis, retention) gates any contact with real data.

## Evidence

- `platform/security/harden.py:12` `wrap_untrusted` — data labelling and escaping;
  `:19` `new_canary`; `:23` `validate_or_reject` rejects on canary leak before schema validation;
  `:31` `evidence_is_exact` — the verifier's substring rule.
- `platform/tests/test_controls.py:21` `test_hardening` — delimiter integrity, canary rejection
  and evidence checking.
- `platform/adapters/claude.py:24` `structured` — cached system prompt, temperature 0 for
  structured output, no tool parameter is ever passed.
- `docs/prd-v2.md` E5–E10 — stages, prompt text, audit chain, golden set and gates.
- **Not implemented — owned by `uc3/P3`–`uc3/P5`:** lexicon YAMLs, triage and deep-analysis
  prompts, the verifier, the append-only hash-chained audit tables, the adversarial golden set.
