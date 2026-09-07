# P8-eval-refactor — Shared eval-harness refactor (optional)

<!-- Run: runbooks/run-prompt.sh program P8-eval-refactor  (or paste into the Codex TUI) -->

Read AGENTS.md. Refactor platform/eval so the three runners share: a common Item/Result schema, a judge utility (Claude-as-judge with cached rubric prompts and a fixed model), cost accounting from adapter spans, and a single `make eval` that runs all three and writes docs/eval/latest.md with deltas versus the previous run. Keep each runner's metrics unchanged. Acceptance: all three eval targets produce identical numbers before and after the refactor (prove it with a before/after diff).
