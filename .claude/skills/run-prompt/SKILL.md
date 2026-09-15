---
name: run-prompt
description: Execute one build prompt from prompts/ end to end without stopping for review — branch, implement, verify against acceptance criteria, report, ship (PR → CI → merge). Use when asked to run, execute, build, or continue a prompt such as "run uc1 P3", "execute the next prompt", or "build UC2 P0-spike through P2".
allowed-tools: Bash(bash scripts/*), Bash(make *), Bash(uv *), Bash(git *), Bash(gh pr *), Read, Edit, Write, Glob, Grep, Agent
argument-hint: "[group] [Pn] — e.g. 'uc1 P3', a range 'uc2 P0-spike..P5', or no argument for the next pending prompt"
---
# Run a build prompt autonomously

Arguments: `<group> <Pn>` where group is one of `program`, `uc1`, `uc2`, `uc3` — for example
`/run-prompt uc1 P3`. A range such as `uc2 P0-spike..P2` runs each prompt in plan order, shipping
each one before starting the next. With no argument at all, run the next prompt whose status is
`pending` and whose prerequisites are all `done` (`bash scripts/run-prompt.sh --next` picks it).

If `scripts/run-prompt.sh` refuses because a prerequisite is not `done`, that is the plan working
as intended: do not override it. Report which prompt must run first and run *that* one instead
(or stop and say so if the caller named a specific prompt for a reason).

## Procedure

1. **Start**: `bash scripts/run-prompt.sh <group> <Pn>`. It verifies prerequisites, creates
   `build/<group>-<pn>` from the fresh base branch, sets the active-prompt marker, and prints the
   prompt text. Read the printed prompt in full. Read `CLAUDE.md` and the rules that apply to the
   paths you will touch (`.claude/rules/`).
2. **Plan in one pass** (no user round-trip): list the files you will add/change, the tests you
   will write, the eval/integration commands that prove each acceptance criterion, and which
   criteria need the Docker stack (`make stack-core` etc.) or live vendor keys. Start the stack
   early in the background if needed (`stack` skill).
3. **Build** domain-first: entities/invariants → adapters/services → API → UI. Keep commits small
   and frequent with plain messages (`feat(uc1): ...`, `test(uc1): ...`). Never leave the branch
   with failing `make check-quick` for long; the PostToolUse hook formats Python as you go.
4. **Verify**: `make check` must be green. Then run exactly the commands the prompt names
   (`make eval-uc1`, `make test-integration`, `make voice-test`, live smoke with
   `LIVE_API_TESTS=1 ... -k <subset>`). Capture the real numbers. Use the `eval-analyst` agent to
   diagnose failing golden items and the `reviewer` agent for a PRD-conformance and security read
   before shipping; act on their findings.
5. **Report**: write `.claude/run/report.md` using the `write-report` skill: each acceptance
   criterion verbatim → PASS / FAIL / UNMEASURED with evidence (command + number + file path).
   Update the prompt's row in `docs/build/PROMPT-PLAN.md` (status `done`, or `partial` with a
   pointer to `docs/build/BLOCKERS.md`). Add ADRs where the prompt or a deviation requires one.
6. **Ship**: commit everything, then `bash scripts/ship.sh`. If CI fails, read
   `.claude/run/ci.log` / `gh run view <id> --log-failed`, fix, commit, and re-run ship. Repeat
   until merged. Ship returns you to the base branch and clears the marker.
7. **Finish**: final message = the report's summary table, merged PR number and SHA, blockers,
   and the next prompt in the plan. If a range was requested, continue with the next prompt.

## Rules of engagement
- Never ask the user a question mid-prompt. Decide, record the decision in the report (and an ADR
  if it is architectural), and keep going. The only reason to end without shipping is a hard
  external blocker (no key, no network, vendor outage) — record it in `docs/build/BLOCKERS.md`
  and ship whatever is complete and green.
- Never edit golden labels, shipped migrations, `docs/prd-v2.md`, `.env*`, or `uv.lock` by hand.
- Live vendor calls cost money: keep them to the subset the prompt requires and print the
  estimated cost before a batch run.
- Keep the PRD's prompt text verbatim where it says "verbatim"; improve behaviour with guards,
  retrieval and schema, and document any prompt-text change with before/after eval numbers.
