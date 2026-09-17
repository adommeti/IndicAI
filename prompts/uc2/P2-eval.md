# P2-eval — Measure the UC2 B6 gates on the real pipeline

<!-- Run: bash scripts/run-prompt.sh uc2 P2-eval -->
Read CLAUDE.md, `docs/prd-v2.md` Part D10 and `apps/training_localizer/eval_hook.py`. `uc2/P2` built
all five pipeline stages, but three of them (`adapt`, `backtranslate_qa`, `quiz`) and the Claude leg
of `post_edit` have never executed against a real vendor, so every UC2 quality gate is either
unmeasured or measured against a stand-in. The Anthropic key that blocked this now resolves as
`INDICAI_ANTHROPIC_API_KEY`. Measure them.

1. Run `make eval-uc2-live` (`--translate training_localizer.eval_hook:full --pre-edit
   training_localizer.eval_hook:translate_only --fidelity-source sut --live`). Capture
   `fidelity_mean` (B6 gate >= 4.0) over the SUT's own output, `terminology_adherence` (gate 1.0)
   both post-edit and pre-edit, and `timing_fit_rate` (D10 target >= 0.90).
2. Diagnose every gate that fails, using the `eval-analyst` agent on the failing golden items. The
   pre-edit adherence number is the one that can move: `post_edit` enforces exactly what the scorer
   tests, so the headline number cannot fail. Say which stage owns each failure.
3. `timing_fit_rate` measured 0.144 without `adapt`. `adapt` is the only stage that compresses, so
   this run is the first real test of whether D7's "shorten wording, not meaning" instruction is
   sufficient. If it is not, report the measured ratio per language against
   `platform/config/timing.yaml` rather than tuning the prompt inside this prompt.
4. Record every number in `docs/build/BLOCKERS.md` against the rows this closes or narrows, and
   update the `uc2/P2` row in `docs/build/PROMPT-PLAN.md`.

Acceptance: `make eval-uc2-live` completes over all 60 segments x 3 languages; `fidelity_mean`,
`terminology_adherence`, `terminology_adherence_pre_edit` and `timing_fit_rate` are each reported
with the item count that produced them; every B6 gate is PASS, FAIL with a diagnosis, or UNMEASURED
with a reason. A failing gate is a valid outcome and must not be tuned away inside this prompt.

## Execution notes
- Needs `INDICAI_ANTHROPIC_API_KEY` and `SARVAM_API_KEY`. No Docker: `eval_hook` calls the stage
  functions in `stages.py` directly, which need neither Postgres nor a broker (the Celery tasks in
  `pipeline.py` do). Redis being down only degrades spend accounting to in-process — note it.
- The runner prints its estimate first; about Rs 110 per full run. Run it once. If a stage dies
  part-way, fix and re-run rather than reporting a partial set as a gate.
- `UC2_EVAL_CONCURRENCY` stays at its default: Sarvam's account limit rejected 4 with 429s that
  outlasted the adapter's retries, and a run that dies two thirds through has spent money and
  measured nothing.
- Do not edit golden labels or the draft reference translations to move a number.
