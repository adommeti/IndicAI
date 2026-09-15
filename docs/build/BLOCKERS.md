# Blockers

Items a build prompt could not complete in-session. One entry per item; remove the entry in the
PR that resolves it.

| date | prompt | what | why | what unblocks it | owner |
|---|---|---|---|---|---|
| 2026-09-15 | program/P1-golden-audio | golden-audio regeneration tool not written | the 135 WAVs were recovered from the developer machine and committed, so the tool was not needed to unblock evals | run `program/P1-golden-audio`, now scoped to just `synthesize_audio.py` | |
| 2026-09-15 | program/P9-adrs | `scripts/ship.sh` cannot complete its PR steps | Two independent causes, both verified in-session. (1) No `gh` on PATH, so ship exits immediately at `ship: gh CLI is required`. (2) Installing `gh` by hand is not sufficient: the session proxy authenticates REST (`gh api repos/{owner}/{repo}/pulls/{n}` returns the PR) but answers GitHub GraphQL with HTTP 403, and `gh pr view`, `gh pr checks` and `gh pr edit`/`gh pr merge` are GraphQL paths — so ship still cannot read the PR, wait for CI, or land it. The branch was pushed and the PR opened through the GitHub MCP server instead; CLAUDE.md allows no other landing path, so the session left the PR for the owner | add `gh` to `scripts/cloud-setup.sh` **and** make ship's PR steps REST-only (`gh api ...`, plus the CCR routes the proxy advertises for auto-merge and ready-for-review); otherwise run ship from a machine with unrestricted GitHub access | |
| 2026-09-15 | program/P9-adrs | PR bodies opened via the GitHub MCP server carry a tool-attribution footer | `.claude/settings.json` sets `attribution.pr=""`, but that governs only the Claude Code CLI's own PR path; the MCP `create_pull_request` call appends the footer server-side, and the `attribution` job rejects it. Editing the body clears it, but CI re-runs replay the original payload | fold a body-hygiene scrub into whatever MCP/REST fallback `scripts/ship.sh` grows, or restore the `gh` CLI so `ship.sh` writes the body itself | |
