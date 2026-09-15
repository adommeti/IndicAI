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

## Redaction

No override is enabled. The platform default (`indic_platform.security.redact`)
applies to every vendor call this app makes.

One consequence worth stating, because it is invisible otherwise: the default
transliteration backend runs **offline**, so no transcript text leaves the
process and no redaction question arises. Setting `SARVAM_TRANSLITERATE=true`
sends segment text to Sarvam, where the default redactor rewrites identifiers
first — so the stored Roman copy can differ from the native text wherever a
phone number or email appears. The native `text` column remains the record of
what was said and is what evidence spans are quoted from.

PRD E's threat table anticipates this app needing an evidence-preserving
override (an off-channel-comms flag may hinge on the phone number itself).
Enabling one is a security decision that belongs with the P7 review, not with
ingestion, so it is deliberately not enabled here.

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
