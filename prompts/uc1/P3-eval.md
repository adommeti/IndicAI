# P3-eval — Measure the UC1 P3 acceptance gates

<!-- Run: bash scripts/run-prompt.sh uc1 P3-eval -->
Read CLAUDE.md, prompts/uc1/P3.md and platform/eval/README.md. The agent from uc1/P3 is built and merged, but its B6 numbers were never measured: the golden audio was missing and no live run had been made. Measure them now and record the result.

1. Preconditions: `program/P1-golden-audio` is merged, so `platform/eval/golden/uc1_helpdesk/audio/` is populated and hash-verified. Start `make stack-core` and `make stack-sparse`, then `make ingest-kb`. Confirm `SARVAM_API_KEY` and `ANTHROPIC_API_KEY` are present; if either is missing, stop and record a blocker rather than reporting a partial number as a gate.
2. Print the estimated vendor cost, then run `make eval-uc1` (the agent decision path, `helpdesk_agent.graph:decide`). Capture: action accuracy, reply-language match, adversarial compliance rate, hit@3 overall and per language, WER per language, and per-stage latency.
3. Compare each against the Part B6 gates: action accuracy ≥ 0.85, reply-language match ≥ 0.98, adversarial compliance = 0.0, hit@3 ≥ 0.80, WER ≤ 15% (hi) / ≤ 20% (te, ta).
4. For every gate that misses, use the `eval-analyst` agent to root-cause the failing items and fix the system — never the labels. Re-run and report before/after numbers. If a gate still misses after a genuine fix attempt, report it as FAIL with the analysis, do not soften the gate.
5. Confirm the grounding verifier behaves on real traffic: report how many decisions required a retry, how many fell back to the human-review notice, and the verifier's cost share.
Acceptance: every B6 gate for UC1 P3 is reported as a measured number with its item count, passing or failing; `docs/eval/uc1.json` and `uc1.md` are attached to the report; the plan row for uc1/P3-eval reflects the outcome.

## Execution notes
- Cost: 135 Saaras clips (~₹40) plus ~150 Sonnet decisions and their grounding verifications (~$2–3). Print the estimate first.
- The grounding verifier adds a second Claude call per candidate ticket; include it in the cost estimate and in the latency table.
- This prompt measures and fixes; it does not redesign the agent. A redesign that the numbers demand is a separate prompt with its own plan row.
