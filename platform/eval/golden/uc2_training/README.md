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
- `quiz_items.jsonl` — 30 items, 10 per module, modelled on the `quiz_items`
  table in PRD D6 with one difference: D6 has `approved boolean default false`
  and these lines carry `status: draft` instead, matching the reference
  translations. `uc2/P2` owns the table; whichever spelling it picks, the
  loader in `run_uc2.py` changes with it. Item ids are unique per
  `(module_id, language, item_id)`, which is D6's key — the same id
  deliberately recurs across modules.

## Everything here is DRAFT

Reference translations carry `status: draft` and an `origin`:

| origin | count per language | meaning |
|---|---:|---|
| `approved_renderings:<id>` | 9 | copied verbatim from the LOCKED statement; correct by construction |
| `mayura:v1 mode=formal` | 21 | machine draft, seeded so the harness can be wired and run |

The `mayura:v1 mode=formal` label records how the 63 machine drafts were produced
during `uc2/P1`, but unlike the `uc2/P0-spike` samples
(`docs/adr/assets/0003/mayura-samples.json`) the raw API responses were not
captured, so the label is not independently checkable from this repo. Treat it as
"machine draft" and nothing stronger.

**A reviewer replaces `reference_text` and flips `status` to `approved`. Nothing
else on the line changes** — that is the whole point of the structure, and
`test_references_carry_replaceable_draft_structure` pins it.

**The drafts break the glossary by construction**, because they are raw
pre-`post_edit` Mayura output and the glossary is applied by the pipeline, not by
the translator. Measured against `apps/training_localizer/terminology/`:

| Language | `keep_english` terms dropped | approved renderings missed |
|---|---:|---:|
| hi-IN | 9 | 3 |
| te-IN | 8 | 4 |
| ta-IN | 8 | 6 |

(hi-IN segment 1 renders GMO as "जी.एम.ओ." against `keep_english: GMO — never
transliterated`.) So these references can never score 100% terminology adherence.
`uc2/P2` must not treat them as the adherence target — the target is the glossary.
They are a fidelity reference, and a provisional one at that.

Until a reviewer approves them the runner reports fidelity as `provisional` and
says so in the report's stage line. The machine drafts are *not* a substitute for the bilingual review PRD
D10 budgets for: they share an engine with the pipeline under test, so scoring the
pipeline against them would be partly circular. They exist so the harness is
runnable and measurable today, not so it can be declared passing.

## What the runner measures

`platform/eval/runners/run_uc2.py` (`make eval-uc2`). See its module docstring
for the full contract. Four conventions worth knowing here:

- **Adherence counts target-language obligations only** — approved renderings and
  LOCKED statements. `keep_english` retention is a separate metric because an
  untranslated baseline satisfies it trivially, and folding it in would put a
  floor under the baseline and hide a broken check.
- **The baseline must score 0% adherence.** `make eval-uc2` runs the untouched
  English source as the "translation"; if that ever scores above zero, the check
  has stopped discriminating and the number means nothing.
- **Adherence is stuffable and fidelity is the answer.** Word salad containing
  every required string scores 100%
  (`test_terminology_adherence_alone_is_stuffable`). That is why B6 gates on
  adherence *and* fidelity >= 4.0, and why a green adherence number on its own
  says nothing about whether the translation reads.
- **The baseline timing number is not a measurement.** Segment durations here
  were authored at the `en-IN` rate in `platform/config/timing.yaml`, so for the
  untranslated baseline the length ratio is the constant `en_cps / lang_cps` and
  the per-language fit rate is 0 or 1, never in between. The runner detects this
  (`timing_durations_synthetic`) and flags the run provisional on its stage line.
  The check discriminates normally on real translations, whose lengths vary.

## A finding from building this

Measured against the timing table in `platform/config/timing.yaml`, the draft
(un-adapted) translations overrun their segment durations by a median of
**1.25x (hi), 1.29x (te), 1.33x (ta)**, so only 17-20% of segments fit within
±15%. That is the harness working: it says the `adapt` stage in PRD D7 — which is
told to shorten wording rather than meaning when a segment is too dense — is
load-bearing for UC2, not optional polish. `uc2/P2` should expect to compress.

## Reviewer worklist: the 8 draft references the judge doubts

The references here are DRAFT placeholders awaiting a human reviewer. Running the
D7 back-translation judge against them (`run_uc2 --live`, 90 references, 180
`claude-haiku-4-5` calls, ~Rs 27 estimated) puts a number on which drafts to look
at first rather than leaving a reviewer to read all 90. It reports
**`fidelity_mean_references` 4.56 / 5** over 90 references; 8 scored below 4. Two
independent runs produced identical scores on all 90 (temperature 0):

| module | seg | language | score | what the back-translation lost or changed |
|---|---|---|---|---|
| comp-201 | 3 | ta-IN | 1 | "Material non-public" came back as "Non-material public" — the obligation is reversed |
| comp-201 | 1 | te-IN | 1 | "insider trading" came back as "benefits of internal trade" — a prohibition read as a benefit |
| comp-201 | 1 | ta-IN | 2 | "insider trading" → "internal trade"; "market conduct" lost its regulatory register |
| comp-201 | 4 | ta-IN | 2 | "press release" → "newspaper publication"; "material" lost its defined sense |
| comp-201 | 6 | ta-IN | 2 | "passing it on" → "smuggling it" — neutral description of tipping turned criminal |
| comp-201 | 8 | ta-IN | 2 | the same-day duty to report to Compliance weakened to merely notifying |
| comp-201 | 3 | te-IN | 2 | "Material non-public information" → "non-private information" |
| sec-102 | 7 | ta-IN | 3 | lost that pasting into Teams is data *leaving* an approved system |

Two things this says, and one it does not.

**It says the drafts are weakest exactly where the cost of being wrong is
highest.** Seven of eight are `comp-201`, the compliance module, and the two
score-1 rows invert a regulatory obligation rather than merely blurring it. A
reader of the Tamil or Telugu draft would come away with the opposite duty.

**It says ta-IN needs the most reviewer time** — six of eight rows, against two
for te-IN and none for hi-IN.

**It does not say UC2 meets the B6 fidelity gate**, and the runner no longer
lets anyone read it that way. This judges the draft *references*, which is what
the P1 prompt asks for ("implement the judge now against the reference
translations"). The B6 gate is about what the pipeline produces, which is
`--fidelity-source sut` and belongs to `uc2/P2`.

That distinction used to live only in prose like this paragraph, while
`docs/eval/uc2.json` published the same number under the gate's own key and
emitted `quality_gates.fidelity_mean: true` — a B6 PASS for UC2 that nothing
about UC2 had earned, and exactly the placeholder passing score
`.claude/rules/eval.md` forbids. The references number now has its own key and
`fidelity_mean` is reported `unmeasured` with its reason, so the two cannot be
confused by a machine reader either
(`test_fidelity_against_references_is_not_reported_as_the_b6_gate`).
