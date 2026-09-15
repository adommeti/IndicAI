#!/usr/bin/env bash
# UserPromptSubmit: attach a one-glance repo status to every prompt so the
# model never has to guess where it is in the build.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
cd "$REPO_ROOT" || exit 0

BRANCH="$(current_branch)"
BASE="$(base_branch)"
AHEAD="$(git rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)"
DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
ACTIVE="none"; [ -f "$STATE_DIR/active-prompt" ] && ACTIVE="$(cat "$STATE_DIR/active-prompt")"
LASTCHECK="never"; [ -f "$STATE_DIR/last-check" ] && LASTCHECK="$(cat "$STATE_DIR/last-check")"
STACK="down"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  UP="$(docker compose --env-file .env.stack ps --status running --format '{{.Service}}' 2>/dev/null | tr '\n' ',' | sed 's/,$//')"
  [ -n "$UP" ] && STACK="up: $UP"
fi

printf '[repo] branch=%s ahead_of_%s=%s dirty=%s active_prompt=%s last_gate=%s stack=%s\n' \
  "$BRANCH" "$BASE" "$AHEAD" "$DIRTY" "$ACTIVE" "$LASTCHECK" "$STACK"
exit 0
