#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit|NotebookEdit): file-level policy.
# - secrets and lockfiles are never edited by hand
# - shipped alembic migrations are immutable
# - golden-set labels that already exist on the base branch are immutable
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
FILE="$(json_get tool_input.file_path)"
[ -n "$FILE" ] || FILE="$(json_get tool_input.notebook_path)"
[ -n "$FILE" ] || exit 0
cd "$REPO_ROOT" || exit 0
BASE="$(base_branch)"

# Make the path repo-relative for matching.
case "$FILE" in
  "$REPO_ROOT"/*) REL="${FILE#"$REPO_ROOT"/}" ;;
  /*) REL="$FILE" ;;
  *) REL="$FILE" ;;
esac

case "$REL" in
  .env|.env.stack|*/.env|*/.env.stack)
    deny "$REL holds credentials and is never edited by the agent. Update .env.example for new variable names." ;;
  uv.lock)
    deny "uv.lock is generated. Run 'uv add ...' or 'uv lock' instead of editing it." ;;
  platform/db/migrations/versions/*)
    if git cat-file -e "origin/$BASE:$REL" 2>/dev/null; then
      deny "$REL is a shipped migration and is immutable. Create a new revision: uv run alembic revision -m '<change>'"
    fi ;;
  platform/eval/golden/*manifest*.jsonl)
    if git cat-file -e "origin/$BASE:$REL" 2>/dev/null; then
      deny "$REL contains committed golden labels. Labels are never changed to pass an evaluation; add a new versioned file and document why."
    fi ;;
  docs/prd-v2.md)
    deny "docs/prd-v2.md is the source specification. Record deviations in docs/adr/ instead of editing the PRD." ;;
esac

exit 0
