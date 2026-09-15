#!/usr/bin/env bash
# PreToolUse(Bash): repository policy for shell commands.
# Denies the small set of actions that would damage history, attribution,
# golden data, or the vendor cost envelope. Everything else passes through to
# the normal permission rules in .claude/settings.json.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
CMD="$(json_get tool_input.command)"
[ -n "$CMD" ] || exit 0
cd "$REPO_ROOT" || exit 0
BASE="$(base_branch)"
BRANCH="$(current_branch)"

# Normalize whitespace for matching; keep the original for messages.
NORM="$(printf '%s' "$CMD" | tr '\n' ' ' | tr -s ' ')"

# --- history and identity -----------------------------------------------------
if printf '%s' "$NORM" | grep -Eq 'git +push( +[^ ]+)* +(--force|-f|--force-with-lease)'; then
  deny "force pushes are not allowed. Rebase onto origin/$BASE and push normally, or open a fresh branch."
fi
if printf '%s' "$NORM" | grep -Eq "git +push( +[^ ]+)* +(origin +)?(refs/heads/)?$BASE( |$)" \
   || { printf '%s' "$NORM" | grep -Eq '(^|[;&| ])git +push( +-u| +--set-upstream)?( +origin)?( +HEAD)?( *$|[;&|])' && [ "$BRANCH" = "$BASE" ]; }; then
  deny "direct pushes to $BASE are not allowed. Work on a feature branch and ship with: bash scripts/ship.sh"
fi
if printf '%s' "$NORM" | grep -Eq 'git +commit( +[^ ]+)* +(--amend|--author|--no-verify|-n( |$))'; then
  deny "git commit --amend/--author/--no-verify are disabled. Make a new commit; repo hooks must run."
fi
if printf '%s' "$NORM" | grep -Eq 'git +(config +(--global|--system|user\.(name|email))|filter-branch|filter-repo|reflog +expire|update-ref +-d)'; then
  deny "changing git identity or rewriting history is disabled. Identity is pinned by .claude/hooks/session-start.sh."
fi
if printf '%s' "$NORM" | grep -Eq 'git +commit' && printf '%s' "$CMD" | grep -Eiq "$FORBIDDEN_TRAILER_RE|co-authored-by|generated with|anthropic"; then
  deny "commit messages must not carry tool attribution trailers or footers. Use a plain conventional message."
fi
# ship.sh drives GitHub over REST (`gh api`), not the GraphQL `gh pr` porcelain,
# so both spellings of "merge a PR" and "open a PR" are guarded.
if printf '%s' "$NORM" | grep -Eq 'gh +pr +merge|(gh +api|-X +PUT)[^|;&]*/pulls/[0-9]+/merge' \
   && [ "${INDICAI_SHIP_ACTIVE:-}" != "1" ]; then
  deny "merge only through scripts/ship.sh, which waits for CI and verifies attribution before merging."
fi
if printf '%s' "$NORM" | grep -Eq 'gh +pr +create|-X +POST[^|;&]*/pulls([^/a-zA-Z]|$)' \
   && [ "${INDICAI_SHIP_ACTIVE:-}" != "1" ]; then
  deny "open PRs through scripts/ship.sh so the PR body is generated from the report and CI is watched."
fi

# --- data and files that must not be destroyed ------------------------------
if printf '%s' "$NORM" | grep -Eq 'rm +(-[a-zA-Z]*r[a-zA-Z]* +|-[a-zA-Z]*f[a-zA-Z]* +)*(\.git|platform/eval/golden|prompts|\.claude|\.githooks|docs/prd-v2\.md|uv\.lock)'; then
  deny "that path is protected (history, golden data, prompts, harness). Remove individual files deliberately instead."
fi
if printf '%s' "$NORM" | grep -Eq 'sed +-i.*platform/db/migrations/versions/'; then
  deny "shipped migrations are immutable. Add a new alembic revision instead."
fi
if printf '%s' "$NORM" | grep -Eq '(^|[;&| ])(pip|pip3) +install'; then
  deny "dependencies are managed by uv. Use: uv add <pkg> [--package <workspace-member>] [--group dev]"
fi
if printf '%s' "$NORM" | grep -Eq 'docker +compose( +[^ ]+)* +down( +[^ ]+)* +(-v|--volumes)'; then
  deny "dropping stack volumes destroys the TEI model cache and databases. Use plain 'docker compose down'."
fi

# --- vendor spend guard: live tests need an explicit opt-in flag --------------
if printf '%s' "$NORM" | grep -Eq 'pytest( +[^ ]+)* +-m +["'"'"']?slow' && ! printf '%s' "$NORM" | grep -q 'LIVE_API_TESTS=1'; then
  deny "live vendor tests must be invoked explicitly: LIVE_API_TESTS=1 uv run pytest -m slow -k <subset>"
fi

exit 0
