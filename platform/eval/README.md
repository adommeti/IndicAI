# Golden sets

P0 includes eight deterministic scaffold fixtures per app: Hindi, Tamil, Telugu,
case-sensitive and empty evidence, escaped delimiters, email, and phone redaction.
`make eval-uc1 eval-uc2 eval-uc3` runs these without vendor calls and writes JSON
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
