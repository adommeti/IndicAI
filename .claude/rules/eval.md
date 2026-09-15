---
paths: ["platform/eval/**", "docs/eval/**"]
---
# Evaluation rules

- Golden data: immutable ids; labels are never edited to pass a run. To fix a wrong label, add a
  new versioned manifest (`manifest.v2.jsonl`) with a `README` note explaining the correction and
  point the runner at it explicitly.
- Every B6 metric is either measured (with the count of items it used) or reported as
  `unmeasured` with the reason. Never emit a placeholder passing score.
- Runners are pluggable: the "system under test" is injected (`--decide module:function`,
  `--detector ...`) so a runner works before its app exists (trivial baseline).
- Reports go to `docs/eval/<uc>.json` + `.md` (gitignored). `report.fails_regression` compares
  against thresholds with explicit direction; the CLI default is strict.
- Live vendor calls in evals are explicit (`--live`, `LIVE_API_TESTS=1`) and print an estimated
  cost before running. Offline CI uses the checked-in scaffold subset and mocked fixtures.
- Adversarial subsets are build-blocking at 0% success; report the exact items that complied.
- Judge calls (Claude-as-judge) use a fixed model id, cached rubric prompt, temperature 0, and the
  rubric text is versioned in `platform/eval/prompts/`.
