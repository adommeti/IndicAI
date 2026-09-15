#!/usr/bin/env bash
# Stop: definition-of-done gate.
# Claude may end its turn only when one of these holds:
#   a) nothing changed (clean tree, no commits ahead of the base branch), or
#   b) the quality gate is green for the current tree AND, if a prompt run is
#      active, the work has been shipped (merged into the base branch).
# Blocking is bounded: after MAX_BLOCKS consecutive blocks the turn is allowed
# to end with a loud warning, so a stuck gate can never loop forever.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
cd "$REPO_ROOT" || exit 0

MAX_BLOCKS="${INDICAI_STOP_MAX_BLOCKS:-4}"
COUNT_FILE="$STATE_DIR/stop-blocks"
COUNT="$(cat "$COUNT_FILE" 2>/dev/null || echo 0)"
BASE="$(base_branch)"
BRANCH="$(current_branch)"

block() {
  echo $((COUNT + 1)) >"$COUNT_FILE"
  printf 'STOP GATE (%s/%s): %s\n' "$((COUNT + 1))" "$MAX_BLOCKS" "$1" >&2
  exit 2
}
allow() { rm -f "$COUNT_FILE"; exit 0; }

git fetch -q origin "$BASE" 2>/dev/null || true
DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
AHEAD="$(git rev-list --count "origin/$BASE..HEAD" 2>/dev/null || echo 0)"

# (a) nothing to gate
if [ "$DIRTY" = "0" ] && [ "$AHEAD" = "0" ]; then allow; fi

# loop bound
if [ "$COUNT" -ge "$MAX_BLOCKS" ]; then
  printf 'STOP GATE: allowed after %s blocks. Work is NOT verified/shipped. State this plainly in the final message.\n' "$COUNT" >&2
  allow
fi

# (b1) quality gate, cached by tree fingerprint
FP="$(tree_fingerprint)"
if [ ! -f "$STATE_DIR/gate-$FP.ok" ]; then
  if bash scripts/checks.sh --gate >"$STATE_DIR/gate.log" 2>&1; then
    touch "$STATE_DIR/gate-$FP.ok"
    date -u +%FT%TZ >"$STATE_DIR/last-check"
  else
    block "quality gate failed. Fix and retry. Tail of .claude/run/gate.log:
$(tail -40 "$STATE_DIR/gate.log")"
  fi
fi

# (b2) uncommitted work is never a stopping point during a prompt run
if [ -f "$STATE_DIR/active-prompt" ]; then
  if [ "$DIRTY" != "0" ]; then
    block "uncommitted changes remain ($DIRTY files) while prompt '$(cat "$STATE_DIR/active-prompt")' is active. Commit them (plain message, no trailers) and ship: bash scripts/ship.sh"
  fi
  if [ "$AHEAD" != "0" ] && [ "$BRANCH" != "$BASE" ]; then
    block "branch $BRANCH has $AHEAD unmerged commit(s) for active prompt '$(cat "$STATE_DIR/active-prompt")'. Ship them: bash scripts/ship.sh  (it pushes, opens the PR, waits for CI, merges). If CI is red, fix and re-run ship."
  fi
fi

allow
