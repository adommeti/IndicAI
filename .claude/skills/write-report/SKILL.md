---
name: write-report
description: Write the definition-of-done report (.claude/run/report.md) that restates a prompt's acceptance criteria with PASS/FAIL/UNMEASURED and evidence, and update the plan row. Use at the end of every prompt run before shipping.
allowed-tools: Read, Write, Edit, Bash(make *), Bash(uv *), Bash(git *)
---
# Definition-of-done report

Write `.claude/run/report.md` in exactly this shape. It is pasted into the PR body, so it must
be self-contained and free of tool names, session links or emoji.

```markdown
### Prompt
<group>/<Pn> — <one-line title>

### Acceptance criteria
| # | Criterion (verbatim from the prompt) | Result | Evidence |
|---|---|---|---|
| 1 | `make eval-uc1` shows action accuracy ≥ 0.85 | PASS | `make eval-uc1` → action_accuracy=0.913 (150 items), docs/eval/uc1.md |
| 2 | adversarial compliance rate = 0.0 | PASS | 0/20 complied |
| 3 | a Langfuse trace exists per turn | UNMEASURED | Langfuse not started in this session (`make stack-obs` not run); unit test asserts span emission |

### Measured numbers
<the metric table printed by the eval/integration commands, verbatim>

### What changed
- <files/modules, one line each, grouped by layer: domain → adapters → api → ui → tests>

### Deviations and decisions
- <anything done differently from the prompt/PRD and why; link ADR ids>

### Not done / blocked
- <item> — <why> — <what unblocks it> (also recorded in docs/build/BLOCKERS.md)

### Commands to reproduce
- `make check` (green at <sha>)
- <every other command that produced the numbers above>
```

Rules:
- A criterion is PASS only with a number or a test name that proves it. "Implemented" is not
  evidence.
- UNMEASURED means the measurement could not run here (stack, keys, hardware). Say what was
  needed. Never substitute a mocked number for a live metric.
- Then update the row for this prompt in `docs/build/PROMPT-PLAN.md`: `status` → `done`
  (all PASS) or `partial` (any FAIL/UNMEASURED that the prompt required), `merged` → filled by
  ship (leave `pending PR`).
