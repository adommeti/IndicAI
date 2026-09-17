# P9-precision — Bring UC3 flag precision to the B6 gate

<!-- Run: bash scripts/run-prompt.sh uc3 P9-precision -->
Read CLAUDE.md, `docs/prd-v2.md` Parts E6 and B6, `.claude/rules/eval.md`, and the uc3/P7 report.
The first live run of the full three-stage detector (2026-09-17) measured **precision 0.30 against a
0.80 gate** over 242 flags and 74 labels, with per-category precision 0.26-0.37 — systematic, not
one bad category. Recall is 0.986 and `clean_flag_rate` is 0.0, so the detector is not flagging
clean calls; it is over-flagging within calls that contain something. Close the gap, or establish
what it would take.

1. Diagnose before changing anything. Use the `eval-analyst` agent over `docs/eval/uc3.json` to
   classify every false positive by root cause: Stage 0 lexicon over-match, Stage 1 triage
   over-escalation, Stage 2 category confusion, evidence-span granularity (one violation reported as
   several flags), or a label the golden set gets wrong. Report the distribution before proposing a
   fix; a precision problem with five different causes does not have one fix.
2. Fix in this order of preference, and say which you used: the combine rule and theta; the
   deterministic Stage 0 floor; retrieval or schema constraints on Stages 1-2. The PRD's E6 prompt
   text ships verbatim — improve behaviour with guards, thresholds and schema, and if you do change
   prompt text, version it and attach before/after eval numbers as `.claude/rules/apps.md` requires.
3. Re-measure after each change with `LIVE_API_TESTS=1 make eval-uc3-full`, and report recall
   alongside precision every time. **Recall must not fall below the 0.85 gate to buy precision** —
   a detector that misses misconduct to look tidy is worse than one that over-flags, and the whole
   point of the reviewer queue is that a human triages. If the two cannot both be met, say so with
   the measured trade-off curve rather than picking one silently.
4. `cost_per_call_inr` is still UNMEASURED even on a live run because the CLI never passes a
   `sink=`. Wire it and report the number; B8's spend argument depends on it.

Acceptance: precision and recall are both re-measured on the full golden set with the item counts
that produced them, and each B6 gate is PASS or FAIL with the measured number. A FAIL with a
diagnosis, a trade-off curve and a named next step is an acceptable outcome; a PASS bought by
editing golden labels, narrowing the scored subset or loosening a threshold in `thresholds.yaml`
without an argument is not.

## Execution notes
- Needs `INDICAI_ANTHROPIC_API_KEY`. Each full run is about $1 / Rs 94 (200 triage plus ~40 deep
  analysis calls); print the estimate first and keep the number of full runs small — iterate on the
  Stage 0 offline gate, which is free, and spend on confirmation.
- **The adversarial-scoring question is a prerequisite decision, not part of this prompt.**
  `adversarial_success` measured 0.15 (3 of 20: `uc3-adv-10/11/12`, all `evasion`, all `echoed` with
  empty `suppressed`), and all three are arguably scoring artifacts — the detector caught both the
  attack turn and the labelled violation, but the `echoed` rule counts a span that is itself
  misconduct as a compliance. Resolving it means either exempting such spans or labelling the attack
  turns in the golden set, and it changes what "0% adversarial success" means. Do not decide it
  inside this prompt: if it is still open, report adversarial as measured-and-disputed with the
  three item ids and carry on with precision.
- Golden-set labels are never edited to make a run pass. If a label is genuinely wrong, add a
  versioned `manifest.v2.jsonl` with a README note, per `.claude/rules/eval.md`, and point the
  runner at it explicitly.
