# Runbook — building this repository with Claude Code on the web

This is the operating procedure for running the prompt sequence in `docs/build/PROMPT-PLAN.md`
as unattended cloud sessions. Everything the session needs is in the repository; the only
one-time work is the cloud environment below.

## 1. One-time: cloud environment

At claude.ai/code → environment selector → **Add cloud environment**:

| Field | Value |
|---|---|
| Name | `indicai` |
| Network access | **Custom**, tick *Also include default list of common package managers*, and add: `api.sarvam.ai`, `docs.sarvam.ai`, `huggingface.co`, `*.huggingface.co`, `cdn-lfs.huggingface.co`, `cdn-lfs-us-1.huggingface.co`, `*.hf.co`, `*.xethub.hf.co`, `ghcr.io`, `github.com`, `objects.githubusercontent.com`, `mcr.microsoft.com`, `aka.ms` |
| Environment variables | `SARVAM_API_KEY=…`, `ANTHROPIC_API_KEY=…`, `ANTHROPIC_MODEL=claude-sonnet-5`, `LIVE_API_TESTS=0`, `INDICAI_GIT_NAME=Anantha Dommeti`, `INDICAI_GIT_EMAIL=<see §4>` |
| Setup script | paste the contents of `scripts/cloud-setup.sh` |

Notes:
- The Anthropic API key must be an environment variable (the proxy never attaches credentials
  to `api.anthropic.com`). The Sarvam key may instead be stored as an *API credential* for host
  `api.sarvam.ai` with header `api-subscription-key` (no prefix); then leave `SARVAM_API_KEY`
  set to any non-empty placeholder so the SDK client constructs.
- The setup script pulls the Compose images and warms the TEI model into the `tei-data` volume;
  it must finish in ~5 minutes and it is cached for ~7 days. If TEI is still downloading when a
  session starts, `scripts/stack.sh wait tei` blocks until it is healthy.
- Memory budget is 16 GB. `make stack-core` (≈ 5 GB) is started automatically in the background
  by the SessionStart hook; add `obs`, `voice`, `sparse` only when a prompt needs them.

## 2. One-time: GitHub

- Connect GitHub in claude.ai/code. For a private repository, install the Claude GitHub App on
  it; alternatively run `/web-setup` from a terminal Claude Code signed into the same account
  (`gh auth refresh -s workflow` first — several prompts change `.github/workflows/`).
- Repository settings → Actions → General: allow GitHub Actions; default workflow permissions
  *Read repository contents* is enough (the CI never writes).
- Branch protection on `main` is optional. If enabled, require the `ci / checks`, `ci /
  attribution` and `ci / integration` checks; do **not** require reviews or the autonomous merge
  in `scripts/ship.sh` will stop at the PR.
- Optional repository variable `ALLOWED_AUTHOR_EMAILS` (comma-separated) if the commit identity
  in §4 changes.

## 3. Start a session

`/run-prompt` exists only once this harness is merged to `main` — a cloud session clones the
repository and nothing else, so a session started against a branch without `.claude/skills/`
answers `Unknown command: /run-prompt`. Confirm with
`git ls-tree --name-only origin/main .claude` before the first run.

The **first** session must be `program P1-golden-audio`: the UC1 golden WAVs are not in git, so
every eval-dependent prompt after it would be unmeasurable. `/run-prompt uc1 P3` deliberately
refuses until that prerequisite is `done` in the plan. `/run-prompt` with no argument always
picks the correct next prompt, so it is the safest thing to type.

Pick repository `adommeti/IndicAI`, branch `main`, environment `indicai`, permission mode
**Auto** (fallback: *Accept edits*; both honor the repo's allow/deny rules). Prompt:

```
/run-prompt program P1-golden-audio
Work autonomously to completion. Do not ask me questions; decide, record decisions in the report,
ship with scripts/ship.sh, and end only when the PR is merged or a hard blocker is recorded in
docs/build/BLOCKERS.md.
```

Range form (sequential within one session, each prompt merged before the next):

```
/run-prompt uc2 P0-spike..P5
Work autonomously through the whole range as above.
```

No-argument form runs the next `pending` prompt whose prerequisites are `done`.

Parallel tracks: start separate sessions for `uc1/…`, `uc2/…`, `uc3/…` once `program/P1-golden-audio`
is merged; each session works on its own branch and `scripts/ship.sh` rebases before merging.

## 4. Attribution

Every commit and PR is authored by the repository owner:
- `.claude/settings.json` sets `attribution.commit=""`, `attribution.pr=""`,
  `attribution.sessionUrl=false`, so no trailer, footer or session link is generated.
- `.claude/hooks/session-start.sh` pins `user.name`/`user.email` for the checkout and installs
  `.githooks/` (`commit-msg` strips any attribution line; `pre-push` refuses direct pushes to
  `main` and re-checks the range).
- `scripts/ship.sh` runs `scripts/attribution-check.sh` before pushing and generates the PR body
  from the report; CI (`attribution` job) rejects any commit or PR body that slips through.
- Squash merges are performed with `gh pr merge` under your GitHub identity, so the commit on
  `main` is authored by you; GitHub records `noreply@github.com` as committer.

Applying this harness with `git am` records **you** as the author (it is in the patch) and your
local `user.email` as the committer. The attribution check is strict about the author and only
rejects tool identities (claude/anthropic/copilot/bot) as the committer, so any personal or
GitHub identity there is fine. If your local git identity is unset, set it before applying.

Identity, as observed on the first real merge: branch commits are authored
`Anantha Dommeti <anantha.dommeti@users.noreply.github.com>`, and the squash-merge commit GitHub
writes on `main` is authored by the merging account, `adommeti <asdommeti@gmail.com>`, with
`GitHub <noreply@github.com>` as committer and a `Co-authored-by` trailer naming the branch author.
All three are expected. The allowlist covers both addresses; set the `ALLOWED_AUTHOR_EMAILS`
repository variable (comma-separated) to change it. Because the merge commits carry the account's
own verified address, they do link to the profile and contribution graph.

The check is strict about the **author** and lenient about the mechanical parts: the committer and
any co-author are rejected only when they are tool identities (claude, anthropic, copilot, `[bot]`).
A blanket ban on `Co-authored-by` would fail every GitHub squash merge.

## 5. What a session does (for reference)

`run-prompt` → branch `build/<uc>-<pn>` → implement → `make check` (Stop gate enforces it) →
prompt-specific evals/integration/live checks → `.claude/run/report.md` → plan row updated →
commit → `scripts/ship.sh` (gate → rebase → push → PR → CI wait → squash-merge → back on `main`).
If CI is red the session fixes and re-ships. If something is impossible, it records the blocker
and ships what is green.

## 6. Local equivalent

`bash scripts/dev-setup.sh` once (hooks, `.env`, `.env.stack`, `uv sync`), then the same
commands. Local Claude Code sessions read the same `CLAUDE.md`, rules, skills, hooks and
settings; `CLAUDE.local.md` and `.claude/settings.local.json` are gitignored for personal
overrides.

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Unknown command: /run-prompt` | The session's branch has no `.claude/skills/`. Merge this harness to `main` (or start the session from the branch that carries it). |
| `run-prompt: prerequisite <x> is 'pending', not done` | Working as intended. Run `<x>` first, or `/run-prompt` with no argument to let the plan choose. |
| Session ends without shipping, message says "STOP GATE allowed after 4 blocks" | The gate kept failing; read `.claude/run/gate.log` in the PR branch, or re-open the session and say "fix the gate and ship". |
| `ship: no CI checks registered` | Actions disabled, or the workflow file changed in this PR and the token lacks the `workflow` scope. |
| Push rejected: refusing to allow … workflow | Reconnect GitHub with a token that has `workflow` scope (`/web-setup` after `gh auth refresh -s workflow`). |
| `attribution-check` FAIL on `main` push | The squash-merge author email is not in `ALLOWED_AUTHOR_EMAILS`; add your GitHub-verified address. |
| TEI unhealthy for >15 min | Weights still downloading; check `docker compose --env-file .env.stack logs tei`; verify `huggingface.co`/`*.hf.co` are allowlisted. |
| `docker info` fails | Docker not available in the environment; stack-dependent criteria are reported UNMEASURED. |
| Sarvam 401/402 or Anthropic credit errors | Key/credit issue; the session records a blocker and ships the mocked path. |
