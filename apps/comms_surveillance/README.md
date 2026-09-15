# comms_surveillance

Batch pipeline for PRD Section E: recorded calls in, diarized and transliterated
transcripts out, hybrid detection and a human review queue on top.

As of uc3/P2 this package contains ingestion and transcription only. Detection
(P3, P4), the audit chain (P5) and the reviewer UI (P6) are subsequent prompts.

## Data classification and retention

Recordings and transcripts are **confidential**. Everything in the repository
today is synthetic: PRD E2's Gate 0 scoping questions are unanswered, so no real
call has been or may be processed (E10). Retention is whatever Compliance
specifies in answer to Gate 0 question 3; until then the pilot runs on the
synthetic golden set and nothing is retained beyond it.

## Redaction — this app has a documented override

**Redaction is deliberately OFF for transcript text sent to Claude**, as PRD E9
requires: an off-channel-comms finding can turn on the phone number itself, and
a reviewer handed `[PHONE]` as the evidence cannot act on it. Evidence has to be
verbatim or it is not evidence.

The override is an explicit policy object, not a monkeypatch
(`.claude/rules/adapters.md`): `detector.claude()` constructs the adapter with
`redactor=lambda text: text` and `wrapper=` the `<transcript>` delimiter E6's
system prompt names. The platform default — full redaction — remains in force
everywhere else, including:

- anything reaching a log or a Langfuse trace (spans are metadata-only);
- the Sarvam transliteration path, if `SARVAM_TRANSLITERATE=true`. The default
  transliteration backend is offline, so no transcript text leaves the process
  at all; with Sarvam enabled the stored Roman copy can differ from the native
  text wherever an identifier appears. The native `text` column remains the
  record of what was said and is what evidence spans are quoted from.

What the override does **not** relax: the transcript is still wrapped in
`<transcript>` tags and HTML-escaped so it cannot close its own tag, both system
prompts state it is data rather than instructions, neither stage is given tools,
and every evidence span is verified to be an exact substring before it reaches a
reviewer.

## The analysis harness

Three stages (PRD E5): a deterministic lexicon floor, Haiku triage on every
call, and Sonnet deep analysis on the escalated share. Claude never reaches a
verdict — Stage 2 produces candidate findings and a person decides.

| control | where |
|---|---|
| transcript as untrusted data | `wrap_untrusted(text, "transcript")`, escaped |
| no exfiltration channel | neither stage is passed tools; JSON output only |
| evidence verified | `detector.verify` drops any span that is not an exact substring, and counts it |
| canary | per-process secret in the system prompt; any output containing it discards the **whole** response |
| manipulation as signal | `instruction_like_content` is a flag category, and is additive — it never suppresses another finding |
| deterministic floor | a high-severity lexicon hit survives whatever Stage 2 returns |

`policy.md` is a **DRAFT** owned by Compliance, as are the lexicon YAMLs.

## Configuration

| variable | default | what it does |
|---|---|---|
| `UC3_BUCKET` | `comms-surveillance` | MinIO bucket holding recordings |
| `UC3_PREFIX` | `recordings/` | prefix the nightly sweep watches |
| `SARVAM_TRANSLITERATE` | unset (offline) | `true` routes transliteration through Sarvam |
| `DATABASE_URL` | — | Postgres for `calls` / `transcript_segments` |
| `CELERY_BROKER_URL` | `redis://localhost:6379/0` | beat and worker broker |

## Transliteration quality

Every segment is stored in the script it was spoken in **and** in Roman, because
lexicon matching (P3) has to see Hinglish written either way. `roman_source`
records which backend produced each rendering.

The offline backend (`indic-transliteration`, ISO 15919) is exact for Devanagari
and Telugu. **It is wrong for Tamil**: the script writes one letter for the k/g
pair, and a script-to-script mapping with no phonology cannot choose, so
`வணக்கம்` comes back as `vaṇaghghaṁ` instead of `vaṇakkam` — in every scheme the
library offers. Sarvam is phonological and returns `Vanakkam`. For any corpus
with Tamil in it, set `SARVAM_TRANSLITERATE=true`.

This is pinned by `test_the_offline_backend_is_wrong_for_tamil_and_this_is_recorded`
so it cannot be mistaken for a transcription fault, and `transliterate.KNOWN_WEAK`
names the affected languages in code.

## Vendor access

Only through `indic_platform.adapters`. This package imports no vendor SDK.
