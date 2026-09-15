# ADR NNNN — <short decision title>

- **Status:** Proposed | Accepted | Superseded by ADR-NNNN | Reserved
- **Date:** YYYY-MM-DD
- **Owner:** <prompt or role that owns this decision, e.g. `uc2/P0-spike`, Compliance>
- **Supersedes / relates to:** <ADR ids, or —>

## Context

What forces the decision: the constraint, the alternatives that were live, and where the
requirement comes from (`docs/prd-v2.md` section, a measured number, a vendor limit).
Two or three paragraphs at most. No history lesson.

## Decision

The decision, in the present tense, as a rule someone can apply: "we do X; we do not do Y".
Include the boundary — what the decision explicitly does *not* cover.

## Consequences

What follows, good and bad. Be concrete: what gets cheaper, what gets harder, what is now
load-bearing, what must be built or measured before the decision is safe.

- **Positive:** …
- **Negative / cost:** …
- **Follow-up required:** … (link the prompt in `docs/build/PROMPT-PLAN.md` that owns it)

## Evidence

Links to the code, tests, config and measurements that implement or verify the decision.
Use `path:line` so the link survives review. Anything not yet implemented is marked
"not implemented — owned by `<group>/<Pn>`", never implied to exist.

- `path/to/module.py:NN` — what it does for this decision
- `platform/tests/test_x.py::test_y` — what it proves
- Measurement: command → number, or "unmeasured — needs `<stack/keys>`"
