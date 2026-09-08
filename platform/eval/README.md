# Golden sets

P0 includes eight deterministic scaffold fixtures per app: Hindi, Tamil, Telugu,
case-sensitive and empty evidence, escaped delimiters, email, and phone redaction.
`python -m indic_platform.eval.runners.run --app uc1` and `make eval-uc2 eval-uc3`
run these without vendor calls and write JSON
and Markdown to `docs/eval/`. A failed scaffold gate exits nonzero.
These are utility checks, **not** model injection tests or B6 application acceptance.
Reports explicitly list every unmeasured application gate.

P1 adds `uc1_helpdesk` (150 utterances, 50/language, 20 adversarial), `uc2_training`
(3 scripts × 3 languages with approved translations and 30 quiz items), and
`uc3_surveillance` (200 synthetic transcripts: 60 positive, 120 clean, 20 adversarial).
Use immutable JSONL IDs, language, synthetic/consent provenance, reference text,
expected labels, adversarial category and relative audio paths. Review labels
independently of code; never change labels to pass a failing evaluation. Keep raw
personal data and real recordings out of git. Store large audio in object storage;
check in a checksum manifest. Synthesize initial audio with Bulbul.

Application runners must measure the B6 metrics listed in each scaffold report,
fail on threshold violations, and block on any adversarial success. Missing data
must be reported as unmeasured, never assigned a passing placeholder score.
`report.fails_regression` rejects missing metrics and compares explicit tolerances
with direction per metric. Offline CI uses the checked-in scaffold subset until
P1 supplies approved application data and mocked pipeline fixtures.

## UC1 P1 baseline and P2 retrieval

`make eval-uc1` runs all 150 items with the trivial clarify decision baseline and
P2 paired live hybrid retrieval, translation off and on. It makes
135 live Saaras v3 calls through `SarvamSTT.batch`; `.env` must provide
`SARVAM_API_KEY`. The 15 Hindi Latn items bypass STT and use their text directly.
The 135 Bulbul v3 WAVs must exist in `golden/uc1_helpdesk/audio/`; absent files
fail before paid calls. IDs and SHA-256s must match the audio manifest before any
vendor calls. WAVs are ignored by git; the checksum/provenance manifest
is checked in. The synthetic utterances pass the shared redactor unchanged.

The Make target explicitly selects `--baseline`: successful harness execution
and zero adversarial compliance yield exit 0, even when B6 quality scores fail.
The P2 command additionally gates hit@3 at 80% overall and for each language in
both comparison modes. Action/groundedness/first-audio remain pre-agent exceptions;
this does not accept the agent
for deployment. Calling `python -m indic_platform.eval.runners.run_uc1 --strict`
(also the default CLI mode) fails on any failed or unmeasured B6 gate.
Offline CI retains scaffold checks and runs the mocked UC1 tests without credentials.

Plug in decisions with `--decide package.module:function`. The callable receives
`(utterance, language, history)` with a fresh history per item and returns a
validated `Decision`. `article_ids=None` means retrieval is not wired, while an
empty list means retrieval ran and found nothing. Python callers can inject an
async STT callable and a language detector into `evaluate`.

Reports are `docs/eval/uc1.json` and `uc1.md`; JSON includes per-item transcripts,
audio hashes, model/prompt version, decisions and measured stage latency.
WER is corpus word-error rate per language after NFC, case folding, punctuation
removal and whitespace normalization. No transliteration or spelling correction
is applied. Hindi WER uses 35 audio items; Telugu and Tamil each use 50.
Action and language accuracy use all 150 items. Langid considers its full language
catalogue. Hit@3 uses only items with expected articles and is unmeasured if any
eligible retrieval result is unwired. Adversarial compliance is case-insensitive
substring matching over the 20 attacks' explicit targets, not a semantic safety judge.
Latency p50/p95 use linear interpolation; STT includes upload and inference,
excludes the concurrency queue, and is not time-to-first-audio. Groundedness and
first-audio latency remain unmeasured until those stages exist. Every invocation
transcribes afresh; it does not silently substitute cached or mocked WER.

The adapter uses Sarvam's REST transcription endpoint for non-diarized clips
under 30 seconds, and batch jobs for longer or diarized media. All golden clips
use the short path. This avoids batch-job setup rate limits encountered during
P1; it still uses Saaras v3 and the shared runtime's limiter, retries and cost spans.

### P2 paired experiment

First start TEI, Qdrant and the bge-m3 sparse sidecar (see
`infra/sparse/README.md` for the native fallback when Docker lacks RAM), then run
`make ingest-kb`. The Qdrant client is pinned to the 1.15 minor series to match
the stack's 1.15.5 server. Retrieval preflight checks services and a populated
collection before live STT calls.

The runner transcribes each native-script item once, then feeds the exact same
transcript to both retrieval configurations. Latn items use their original text.
Modes alternate execution order per item; no translated-query cache is used.
`RETRIEVAL__PARALLEL_TRANSLATE` determines which mode fills the selected `hit_at_3`
and decision article IDs, while both configurations appear separately in reports.
Run only the selected mode with `--baseline --retrieval`; omit retrieval flags
to reproduce the P1 clarify-only harness. `--strict` still enforces all B6 gates.

Hit@3 tests the first three returned **chunks** against the expected canonical
article ID or its explicitly declared alias. It does not deduplicate to the first
three articles. The unchanged P1 set has 69 retrieval-labeled items, 23/language;
the remaining 81 still run retrieval but are excluded from hit@3. Both numerator
and denominator are based only on those labels. No labels enter the retriever.

The 250ms budget covers translation plus English embedding and search, including
queue/retry time. Late results are cancelled and discarded, not fused after the
deadline. Reports contain per-item chunks, scores, branch status, elapsed time,
and estimated translation cost for all attempted requests, including timeouts.
The estimate uses the redacted character count and Mayura's pricing config; it is
not proof of billing or a claim that cancelled requests are free. Original-path
latency remains separately visible in each per-item result. Startup/model-loading
time is excluded from retrieval latency. Groundedness and action quality are not
improved or accepted by this retrieval-only experiment.
