# P8-eval-refactor — Shared eval-harness refactor (optional)

<!-- Run: bash scripts/run-prompt.sh program P8-eval-refactor -->
Read CLAUDE.md. Refactor platform/eval so the three runners share: a common Item/Result schema, a judge utility (Claude-as-judge with cached rubric prompts and a fixed model), cost accounting from adapter spans, and a single `make eval` that runs all three and writes docs/eval/latest.md with deltas versus the previous run. Keep each runner's metrics unchanged. Acceptance: all three eval targets produce identical numbers before and after the refactor (prove it with a before/after diff).

## Execution notes
- Prerequisites: uc1/P3, uc2/P2, uc3/P4 merged (all three runners exist with real metrics).
- Prove "identical numbers": run each `make eval-ucN` before and after on the same inputs and diff the JSON metric blocks; include the diff in the report. Live UC1 STT is expensive — reuse the transcripts from the last `docs/eval/uc1.json` via a `--transcripts-from` flag you add, so the before/after comparison is deterministic and free.
- `make eval` must degrade gracefully: unmeasured metrics stay unmeasured; the delta section says "no previous run" the first time.
