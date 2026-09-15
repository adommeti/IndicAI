# UC2 training-localization golden set v1

Three authored English compliance-training modules, 20 timestamped segments each
(60 total, 9 of them LOCKED), with draft reference translations for 30 segments
per language and 30 reference quiz items. No real GMO training content: these are
synthetic scripts written for this harness. IDs and labels must not be changed to
improve a score — see `.claude/rules/eval.md`.

| Module | Topic | Segments | Locked |
|---|---|---:|---:|
| `sec-101` | Phishing and credential security | 20 | 4 |
| `sec-102` | Handling client and personal data | 20 | 3 |
| `comp-201` | Insider trading and conflicts of interest | 20 | 2 |

## Files

- `samples/<module>.jsonl` — one segment per line: `module_id`, `seg_id`,
  `start_ms`, `end_ms`, `source_text`, `locked`, and `locked_id` when locked.
  Durations were authored at 15 English chars/second, floored at 3.5 s.
- `reference_translations.<lang>.jsonl` — 30 lines per language
  (10 per module, always including that module's locked segments).
- `quiz_items.jsonl` — 30 items, 10 per module, in the shape of the `quiz_items`
  table in PRD D6.

## Everything here is DRAFT

Reference translations carry `status: draft` and an `origin`:

| origin | count per language | meaning |
|---|---:|---|
| `approved_renderings:<id>` | 9 | copied verbatim from the LOCKED statement; correct by construction |
| `mayura:v1 mode=formal` | 21 | machine draft, seeded so the harness can be wired and run |

**A reviewer replaces `reference_text` and flips `status` to `approved`. Nothing
else on the line changes** — that is the whole point of the structure, and
`test_references_carry_replaceable_draft_structure` pins it.

Until then the runner reports fidelity as `provisional` and says so in the report's
stage line. The machine drafts are *not* a substitute for the bilingual review PRD
D10 budgets for: they share an engine with the pipeline under test, so scoring the
pipeline against them would be partly circular. They exist so the harness is
runnable and measurable today, not so it can be declared passing.

## What the runner measures

`platform/eval/runners/run_uc2.py` (`make eval-uc2`). See its module docstring
for the full contract. Two conventions worth knowing here:

- **Adherence counts target-language obligations only** — approved renderings and
  LOCKED statements. `keep_english` retention is a separate metric because an
  untranslated baseline satisfies it trivially, and folding it in would put a
  floor under the baseline and hide a broken check.
- **The baseline must score 0% adherence.** `make eval-uc2` runs the untouched
  English source as the "translation"; if that ever scores above zero, the check
  has stopped discriminating and the number means nothing.

## A finding from building this

Measured against the timing table in `platform/config/timing.yaml`, the draft
(un-adapted) translations overrun their segment durations by a median of
**1.25x (hi), 1.29x (te), 1.33x (ta)**, so only 17-20% of segments fit within
±15%. That is the harness working: it says the `adapt` stage in PRD D7 — which is
told to shorten wording rather than meaning when a segment is too dense — is
load-bearing for UC2, not optional polish. `uc2/P2` should expect to compress.
