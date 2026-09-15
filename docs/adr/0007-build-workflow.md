# ADR 0007 — Build workflow: one PR per prompt, squash-merged, gated by CI

- **Status:** Accepted
- **Date:** 2026-09-15 · **Owner:** `program/P0`
- **Relates to:** ADR 0008 (the environment this workflow runs in), ADR 0002

## Context

This repository is built by autonomous agent sessions, not by a person working a backlog. Nobody
is watching a session while it runs, it cannot ask a question mid-flight, and "it looked fine" is
not available as a landing criterion: whatever catches a mistake has to be mechanical and has to
run before the work reaches `main`. A long-lived branch accumulating several prompts would make a
failure hard to attribute and a revert expensive, and merge commits would make the history
unreadable as a build log.

## Decision

**One prompt, one branch, one PR, squash-merged.** `scripts/run-prompt.sh <group> <Pn>` verifies
the prompt's prerequisites are `done` in `docs/build/PROMPT-PLAN.md`, cuts `build/<group>-<pn>`
from a freshly fetched base, and sets the active-prompt marker. `scripts/ship.sh` is the only
sanctioned way to land work: gate → rebase onto the base → push → open or reuse the PR → wait for
CI → squash → return to the base branch. Landing a PR any other way is blocked by a Bash guard,
and `.githooks/pre-push` refuses a direct push to `main`.

**Sequencing lives in `docs/build/PROMPT-PLAN.md`**, not in an agent's head. Each row carries
`status`, `needs` (prerequisite prompts), verification tiers and the merge SHA. A prompt whose
prerequisite is not `done` does not start; that refusal is the plan working, not an obstacle to
route around. Only the row being shipped changes in a PR. Independent tracks may run as parallel
sessions; `ship.sh`'s rebase reconciles the shared files, and two alembic heads get a merge revision.

**Three CI jobs gate every PR** (`.github/workflows/ci.yml`):

- `attribution` — every commit is authored by the repository owner; messages are plain and
  conventional, with no trailers, emoji, tool names or session links; the PR body is checked too.
- `checks` — lint, format, typecheck, unit tests, the three offline eval runners, the UI packages
  when present, a `pip-audit` SBOM, and a `detect-secrets` scan. The local `make check` gate is a
  subset: it skips the UI packages, and runs `pip-audit` only under `--full`.
- `integration` — Postgres/pgvector, Redis and Qdrant service containers; `alembic upgrade head`
  then `alembic check`, then the integration-marked tests.

**Three verification tiers**, recorded per prompt in the plan's `verify` column:

| Tier | What it is | How it runs |
|---|---|---|
| **S** — in-session | lint, typecheck, unit tests, offline evals, attribution, secrets | `make check` (`scripts/checks.sh --gate`); the Stop gate enforces it before a session ends |
| **C** — CI service containers | migrations round-trip and integration tests against real Postgres/Redis/Qdrant | the `integration` job, or `make test-integration` locally |
| **K/L** — full stack and live vendors | `make stack-*` profiles, `make eval-ucN`, `make voice-test`, `LIVE_API_TESTS=1` smoke | in-session with Docker and vendor keys; never in CI, which pins `LIVE_API_TESTS=0` |

Every prompt ends with `.claude/run/report.md` restating each acceptance criterion as
PASS / FAIL / UNMEASURED with a number or a test name; `ship.sh` uses it as the PR body. A mocked
number never substitutes for a live one.

## Consequences

- **Positive:** every merge is one prompt, so `git log` on `main` reads as the build plan, and a
  bad prompt reverts as one commit.
- **Positive:** the same gate runs in-session and in CI, so a locally green session is almost
  always green in CI; the PR body is the verification report, so review starts from evidence.
- **Negative:** tier K/L cannot run in CI, so the most expensive claims (WER, hit@3, adversarial
  rate) are verified only in a session with Docker and keys, and stay UNMEASURED until one runs
  them — which is why `program/P1-golden-audio` is `partial` in the plan.
- **Negative:** squash plus rebase-before-push rewrites branch history; parallel sessions must not
  share a branch.
- **Negative:** CI green is a landing precondition, so a flaky job blocks the autonomous loop; the
  session's recourse is to fix and re-ship, never to route around `ship.sh`.
- **Follow-up required:** branch protection on `main` is optional today; if enabled it must require
  the three checks and **not** require reviews, or the autonomous loop stops at the PR.

## Evidence

- `scripts/run-prompt.sh` — prerequisite check, branch, marker; `scripts/ship.sh:61-193` — gate,
  rebase, attribution re-check, PR, CI wait and squash, every GitHub call over REST (`gh api`).
- `scripts/checks.sh:20-38` — gate stages and the `--full` tier; `Makefile` — `check`,
  `check-full`, `test-integration`, `eval-uc1|uc2|uc3`, `voice-test`.
- `.github/workflows/ci.yml` — the three jobs and `LIVE_API_TESTS: '0'`.
- `scripts/attribution-check.sh`, `.githooks/`, `.claude/hooks/stop-gate.sh`,
  `.claude/hooks/guard-bash.sh` — the local half of the gates.
- `docs/build/PROMPT-PLAN.md` — sequencing table and tier legend; `docs/build/RUNBOOK.md` — the
  session operating procedure; `CLAUDE.md` — the agent's working agreement.
