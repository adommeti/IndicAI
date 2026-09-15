# ADR 0001 — Claude reasons in the user's language

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `program/P0`, applied by `uc1/P2`, `uc1/P3`
- **Relates to:** ADR 0002 (adapters are the vendor boundary)

## Context

`docs/prd-v2.md` B2 is the program's most consequential design choice. v1 assumed a
translate-in / reason-in-English / translate-out sandwich. That costs two Mayura hops on every
conversational turn, puts translation quality on the critical path of a sub-second voice budget,
and softens the user's own words before Claude ever sees them.

Sonnet 5 is natively multilingual for the pilot set (Hindi, Telugu, Tamil, code-mixed Hinglish),
so the translation hop buys nothing on the conversational path. It still earns its place in two
narrow spots: producing an English artifact, and widening recall when a non-English query is run
against an English-only knowledge base.

## Decision

Claude reasons and replies in the language and script the user used. There is no translate-in or
translate-out step on any conversational path.

Translation is used in exactly three places:

1. **English artifacts** — tickets, case records and reviewer-facing summaries are written in
   English *by Claude directly*, as fields of the same structured output. No separate call.
2. **Optional retrieval booster** — a Mayura-translated copy of the query may run as a second,
   parallel retrieval pass, fused with the native-script pass by reciprocal rank fusion. It is
   off by default, deadline-bounded, and turned on only if the eval harness shows it pays.
3. **UC2, where the translation is itself the product** — Mayura `formal` mode plus Claude
   back-translation QA. Out of scope for this ADR.

Flagged-evidence rendering in UC3 (verbatim span plus an English rendering) is covered by
ADR 0005, not here.

## Consequences

- **Positive:** one vendor call per turn instead of three; Mayura is not on the voice latency
  budget; the reviewer sees the employee's own words, not a round-trip paraphrase.
- **Positive:** retrieval quality is decoupled from translation quality — the booster is an
  experiment with an off switch, not an architectural dependency.
- **Negative:** reply-language correctness now depends on the model, so it needs a deterministic
  check. A guard rejects a turn whose reply language does not match the user's; Hinglish is
  treated as a script variant of Hindi rather than a separate language.
- **Negative:** an English-only KB is now interrogated cross-lingually by `bge-m3` alone in the
  default configuration. If hit@3 misses its 80% gate, the booster is the first lever.
- **Follow-up required:** B2's fallback policy — reply in English and log when auto-detect returns
  a language outside the pilot set — is only enforced at the chat API boundary today (the request
  schema rejects it). The speech path that can actually auto-detect is owned by `uc1/P5`.

## Evidence

- `apps/helpdesk_agent/prompts/decide.md:4` — reply in the employee's language and script;
  `:8` — English reference articles, substance translated in-line; `:14` — ticket written in English.
- `apps/helpdesk_agent/graph.py:143` `language_matches` and `:196` the `reply_language` guard
  error — the deterministic check the decision requires. `:88` `roman_hindi` handles Hinglish.
- `apps/helpdesk_agent/retriever.py:69-121` — the optional parallel translate pass, its deadline,
  its cancellation of late results, and `rrf` fusion.
- `platform/config/settings.py:14` — `parallel_translate` defaults to `False`; `:15` — the
  translate deadline is capped at 250 ms.
- `platform/eval/runners/run_uc1.py:427-459` — the paired A/B that decides the booster:
  one transcription per item fed to both configurations, reported as `hit_at_3_off` /
  `hit_at_3_on`, overall and per language. `platform/tests/test_retrieval.py:231-234` proves the
  paired metric wiring.
- **Measurement: unmeasured.** `docs/eval/uc1.json` is a generated artifact and is not in the
  repository (`.gitignore:17`); no UC1 P2 paired run has been executed with the stack and live
  Saaras yet. The with/without hit@3 comparison is owned by `uc1/P3-eval`
  (`docs/build/PROMPT-PLAN.md`). No placeholder number is recorded here.
