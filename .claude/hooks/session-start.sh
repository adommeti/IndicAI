#!/usr/bin/env bash
# SessionStart: pin the commit identity, install repo git hooks, sync deps,
# and (in cloud sessions) warm the local service stack in the background.
# Prints a short orientation block that Claude sees at the start of the session.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
SOURCE="$(json_get source)"
cd "$REPO_ROOT" || exit 0

# 1. Identity and git hooks. Local config only; never touches ~/.gitconfig.
git config --local user.name "$GIT_NAME"
git config --local user.email "$GIT_EMAIL"
git config --local commit.gpgsign false
git config --local core.hooksPath .githooks
git config --local pull.rebase false
git config --local push.autoSetupRemote true
chmod +x .githooks/* .claude/hooks/*.sh scripts/*.sh 2>/dev/null || true

# 2. Dependencies (fast no-op when the lock is already synced).
if command -v uv >/dev/null 2>&1 && [ "$SOURCE" != "compact" ]; then
  uv sync --frozen --all-packages >"$STATE_DIR/uv-sync.log" 2>&1 || echo "uv sync failed; see .claude/run/uv-sync.log" >&2
fi

# 3. Local-only stack credentials (idempotent; file is gitignored).
[ -f .env.stack ] || python3 infra/bootstrap.py >/dev/null 2>&1 || true

# 4. Cloud sessions: warm the core service stack in the background so retrieval
#    and integration work does not wait on it. Full stack via `make up`.
STACK_NOTE="stack: not started (run \`make stack-core\` or \`make up\`)"
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] && [ "${INDICAI_AUTOSTART_STACK:-1}" != "0" ] \
   && command -v docker >/dev/null 2>&1 && { [ "$SOURCE" = "startup" ] || [ "$SOURCE" = "resume" ]; }; then
  if docker info >/dev/null 2>&1; then
    nohup bash scripts/stack.sh core >"$STATE_DIR/stack.log" 2>&1 &
    STACK_NOTE="stack: core services starting in background (log: .claude/run/stack.log; \`make stack-status\`)"
  else
    STACK_NOTE="stack: docker daemon not available"
  fi
fi

# 5. Orientation for Claude (plain stdout is injected as context).
BRANCH="$(current_branch)"
BASE="$(base_branch)"
git fetch -q origin "$BASE" 2>/dev/null || true
AHEAD="$(git rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)"
DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
KEYS=""
[ -n "${SARVAM_API_KEY:-}" ] && KEYS="$KEYS SARVAM_API_KEY" || KEYS="$KEYS !SARVAM_API_KEY"
[ -n "${ANTHROPIC_API_KEY:-}" ] && KEYS="$KEYS ANTHROPIC_API_KEY" || KEYS="$KEYS !ANTHROPIC_API_KEY"
ACTIVE=""
[ -f "$STATE_DIR/active-prompt" ] && ACTIVE="$(cat "$STATE_DIR/active-prompt")"

cat <<EOF
[indicai session] branch=$BRANCH base=$BASE ahead=$AHEAD dirty_files=$DIRTY remote=${CLAUDE_CODE_REMOTE:-false}
[indicai session] commit identity pinned to "$GIT_NAME <$GIT_EMAIL>"; repo git hooks active (.githooks)
[indicai session] vendor keys present:$KEYS  ("!" = missing → live steps must be skipped and reported)
[indicai session] $STACK_NOTE
[indicai session] active prompt marker: ${ACTIVE:-none}. Plan: docs/build/PROMPT-PLAN.md. Runbook: docs/build/RUNBOOK.md
[indicai session] To execute a prompt end to end (branch → build → verify → PR → CI → merge) use the run-prompt skill: /run-prompt <uc> <Pn>
EOF
exit 0
