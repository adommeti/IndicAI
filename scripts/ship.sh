#!/usr/bin/env bash
# Ship the current branch: gate → rebase → push → PR → wait for CI → squash-merge.
#
#   scripts/ship.sh [--title "<pr title>"] [--body-file <path>] [--no-merge] [--draft]
#                   [--ci-timeout <seconds>]
#
# Idempotent: re-running after a CI failure reuses the open PR. The merge step
# is the only place a PR is merged in this repository (the Bash guard blocks
# direct `gh pr merge`), so CI green and clean attribution are always enforced.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
source .claude/hooks/_lib.sh
export INDICAI_SHIP_ACTIVE=1

TITLE=""; BODY_FILE=""; MERGE=1; DRAFT=""; CI_TIMEOUT="${INDICAI_CI_TIMEOUT:-3600}"
while [ $# -gt 0 ]; do
  case "$1" in
    --title) TITLE="$2"; shift 2 ;;
    --body-file) BODY_FILE="$2"; shift 2 ;;
    --no-merge) MERGE=0; shift ;;
    --draft) DRAFT="--draft"; MERGE=0; shift ;;
    --ci-timeout) CI_TIMEOUT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

BASE="$(base_branch)"
BRANCH="$(current_branch)"
fail() { printf 'ship: %s\n' "$1" >&2; exit 1; }

[ "$BRANCH" != "$BASE" ] && [ "$BRANCH" != "HEAD" ] || fail "you are on '$BRANCH'. Create a feature branch first: git switch -c <type>/<uc>-<pn>-<slug>"
[ -z "$(git status --porcelain)" ] || fail "working tree has uncommitted changes. Commit them with a plain message first."
command -v gh >/dev/null 2>&1 || fail "gh CLI is required"

# 1. Quality gate (cached by fingerprint when the Stop gate already ran it)
FP="$(tree_fingerprint)"
if [ ! -f "$STATE_DIR/gate-$FP.ok" ]; then
  echo "ship: running quality gate"
  bash scripts/checks.sh --gate >"$STATE_DIR/gate.log" 2>&1 || { tail -40 "$STATE_DIR/gate.log" >&2; fail "quality gate failed (see .claude/run/gate.log)"; }
  touch "$STATE_DIR/gate-$FP.ok"
fi

# 2. Bring the branch up to date with the base branch
git fetch -q origin "$BASE" || fail "cannot fetch origin/$BASE"
if [ "$(git rev-list --count "HEAD..origin/$BASE")" != "0" ]; then
  echo "ship: rebasing onto origin/$BASE"
  git rebase -q "origin/$BASE" || { git rebase --abort; fail "rebase conflicts with origin/$BASE. Resolve manually (git rebase origin/$BASE), then re-run ship."; }
fi
AHEAD="$(git rev-list --count "origin/$BASE..HEAD")"
[ "$AHEAD" != "0" ] || fail "no commits ahead of origin/$BASE; nothing to ship"

# 3. Attribution is verified before anything leaves the machine
bash scripts/attribution-check.sh "origin/$BASE..HEAD" || fail "attribution check failed; rewrite the offending commit messages (git rebase is blocked: create a new branch from origin/$BASE and cherry-pick with clean messages)"

# 4. Push
git push -u origin "HEAD:$BRANCH" || fail "push failed"

# 5. Open or reuse the PR
ACTIVE="$(cat "$STATE_DIR/active-prompt" 2>/dev/null || true)"
[ -n "$TITLE" ] || TITLE="$( [ -n "$ACTIVE" ] && printf '%s: ' "$ACTIVE"; git log -1 --format=%s "origin/$BASE..HEAD" | head -1)"
[ -n "$TITLE" ] || TITLE="$(git log -1 --format=%s)"
TITLE="$(printf '%s' "$TITLE" | cut -c1-120)"

BODY_TMP="$(mktemp)"
if [ -n "$BODY_FILE" ] && [ -f "$BODY_FILE" ]; then
  cp "$BODY_FILE" "$BODY_TMP"
else
  {
    echo "## Summary"
    git log --reverse --format='- %s' "origin/$BASE..HEAD"
    echo
    if [ -f "$STATE_DIR/report.md" ]; then
      echo "## Verification"
      cat "$STATE_DIR/report.md"
    else
      echo "## Verification"
      echo "- \`scripts/checks.sh --gate\` green on $(git rev-parse --short HEAD)"
    fi
  } >"$BODY_TMP"
fi
# The PR body must be as clean as the commits.
grep -Eiq "$FORBIDDEN_TRAILER_RE" "$BODY_TMP" && fail "PR body contains a tool-attribution line"

PR_NUM="$(gh pr view "$BRANCH" --json number --jq .number 2>/dev/null || true)"
if [ -z "$PR_NUM" ]; then
  # shellcheck disable=SC2086
  gh pr create --base "$BASE" --head "$BRANCH" --title "$TITLE" --body-file "$BODY_TMP" $DRAFT >"$STATE_DIR/pr-create.log" 2>&1 \
    || { cat "$STATE_DIR/pr-create.log" >&2; fail "gh pr create failed"; }
  PR_NUM="$(gh pr view "$BRANCH" --json number --jq .number)"
  echo "ship: opened PR #$PR_NUM"
else
  gh pr edit "$PR_NUM" --title "$TITLE" --body-file "$BODY_TMP" >/dev/null 2>&1 || true
  echo "ship: reusing PR #$PR_NUM"
fi
rm -f "$BODY_TMP"
PR_URL="$(gh pr view "$PR_NUM" --json url --jq .url)"

[ "$MERGE" = "1" ] || { echo "ship: PR ready (not merged): $PR_URL"; exit 0; }

# 6. Wait for CI. Checks can take a moment to register after the push.
echo "ship: waiting for checks on PR #$PR_NUM (timeout ${CI_TIMEOUT}s)"
START=$(date +%s)
until [ "$(gh pr checks "$PR_NUM" --json name --jq 'length' 2>/dev/null || echo 0)" != "0" ]; do
  [ $(( $(date +%s) - START )) -lt 300 ] || fail "no CI checks registered on PR #$PR_NUM after 5 minutes. Is the workflow enabled? $PR_URL"
  sleep 15
done
if ! timeout "$CI_TIMEOUT" gh pr checks "$PR_NUM" --watch --fail-fast --interval 20 >"$STATE_DIR/ci.log" 2>&1; then
  cat "$STATE_DIR/ci.log" >&2
  echo "ship: CI failed on PR #$PR_NUM ($PR_URL)." >&2
  echo "ship: inspect with: gh pr checks $PR_NUM ; gh run view <run-id> --log-failed" >&2
  exit 1
fi
echo "ship: CI green"

# 7. Merge as the repository owner; the squash commit carries the PR author identity.
MERGE_BODY="$(git log --reverse --format='- %s' "origin/$BASE..HEAD")"
gh pr merge "$PR_NUM" --squash --delete-branch --subject "$TITLE" --body "$MERGE_BODY" >"$STATE_DIR/merge.log" 2>&1 \
  || { cat "$STATE_DIR/merge.log" >&2; fail "merge failed for PR #$PR_NUM ($PR_URL)"; }
echo "ship: merged PR #$PR_NUM into $BASE ($PR_URL)"

# 8. Return to an up-to-date base branch and clear the prompt marker.
git fetch -q origin "$BASE"
git switch -q "$BASE" 2>/dev/null || git checkout -q "$BASE"
git reset -q --hard "origin/$BASE"
git branch -D "$BRANCH" >/dev/null 2>&1 || true
rm -f "$STATE_DIR/active-prompt" "$STATE_DIR/report.md" "$STATE_DIR"/gate-*.ok "$STATE_DIR/stop-blocks"
MERGED_SHA="$(git rev-parse --short HEAD)"
echo "ship: $BASE is now at $MERGED_SHA"
exit 0
