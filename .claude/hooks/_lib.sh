#!/usr/bin/env bash
# shellcheck disable=SC2034
# Shared helpers for repo hooks. Sourced, never executed directly.
# Everything here must be safe under `set -euo pipefail` and must not print
# unless a caller asks for output.

REPO_ROOT="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
STATE_DIR="$REPO_ROOT/.claude/run"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# The committer identity every change in this repository is recorded under.
# Override with INDICAI_GIT_NAME / INDICAI_GIT_EMAIL if the owner's identity changes.
GIT_NAME="${INDICAI_GIT_NAME:-Anantha Dommeti}"
GIT_EMAIL="${INDICAI_GIT_EMAIL:-anantha.dommeti@users.noreply.github.com}"

# Lines that must never appear in a commit message or PR body.
# Kept as an ERE so commit-msg, pre-push, CI and the Bash guard agree.
FORBIDDEN_TRAILER_RE='^(Co-Authored-By|Co-authored-by|Claude-Session|Generated-With|Generated-with):|Generated with \[?Claude|🤖|Claude-Session|noreply@anthropic\.com|Co-Authored-By: *Claude'

# Read the hook's JSON payload from stdin into HOOK_INPUT (may be empty).
read_hook_input() {
  if [ -t 0 ]; then HOOK_INPUT=""; else HOOK_INPUT="$(cat || true)"; fi
}

# json_get <jq-path>  — prints the value at path from HOOK_INPUT, or empty.
json_get() {
  [ -n "${HOOK_INPUT:-}" ] || { echo ""; return 0; }
  printf '%s' "$HOOK_INPUT" | python3 -c '
import json, sys
path = sys.argv[1].split(".")
try:
    obj = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)
for key in path:
    if isinstance(obj, dict) and key in obj:
        obj = obj[key]
    else:
        print(""); sys.exit(0)
if isinstance(obj, (dict, list)):
    print(json.dumps(obj))
elif obj is None:
    print("")
else:
    print(obj)
' "$1" 2>/dev/null || echo ""
}

# deny <reason>  — PreToolUse: block the tool call. Exit 2 + stderr is the
# documented blocking contract and works on every Claude Code version.
deny() {
  printf 'BLOCKED by repo policy: %s\n' "$1" >&2
  exit 2
}

# allow_json  — PreToolUse: explicitly pre-approve (skips prompts/classifier).
allow_json() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"%s"}}\n' "${1:-repo policy}"
  exit 0
}

# Current branch name or "HEAD" when detached.
current_branch() { git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD"; }

# Base branch the project merges into.
base_branch() {
  local b
  b="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
  echo "${b:-main}"
}

# Fingerprint of the working tree + HEAD, for caching check results.
tree_fingerprint() {
  {
    git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null
    git -C "$REPO_ROOT" diff HEAD 2>/dev/null
    git -C "$REPO_ROOT" ls-files --others --exclude-standard 2>/dev/null | while read -r f; do
      [ -f "$REPO_ROOT/$f" ] && sha256sum "$REPO_ROOT/$f" 2>/dev/null
    done
  } | sha256sum | cut -c1-16
}
