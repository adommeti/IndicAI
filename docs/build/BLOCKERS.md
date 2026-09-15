# Blockers

Items a build prompt could not complete in-session. One entry per item; remove the entry in the
PR that resolves it.

| date | prompt | what | why | what unblocks it | owner |
|---|---|---|---|---|---|
| 2026-09-15 | program/P1-golden-audio | golden-audio regeneration tool not written | the 135 WAVs were recovered from the developer machine and committed, so the tool was not needed to unblock evals | run `program/P1-golden-audio`, now scoped to just `synthesize_audio.py` | |
| 2026-09-15 | program/P9-adrs | autonomous merge step: `scripts/ship.sh` cannot run | the cloud environment has no `gh` CLI (`ship: gh CLI is required`); GitHub access in this session is via the GitHub MCP server instead. The branch was pushed and the PR opened through MCP, but CLAUDE.md forbids merging by any path other than `ship.sh`, so the session did not merge | either install `gh` in the environment (add it to `scripts/cloud-setup.sh` and the network allowlist) or teach `scripts/ship.sh` an MCP/REST fallback for PR create, CI wait and squash-merge; until then the owner marks the PR ready and merges | |
| 2026-09-15 | program/P9-adrs | PR bodies opened via the GitHub MCP server carry a tool-attribution footer | `.claude/settings.json` sets `attribution.pr=""`, but that governs only the Claude Code CLI's own PR path; the MCP `create_pull_request` call appends the footer server-side, and the `attribution` job rejects it. Editing the body clears it, but CI re-runs replay the original payload | fold a body-hygiene scrub into whatever MCP/REST fallback `scripts/ship.sh` grows, or restore the `gh` CLI so `ship.sh` writes the body itself | |
