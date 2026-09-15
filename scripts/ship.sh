#!/usr/bin/env bash
# Ship the current branch: gate → rebase → push → PR → wait for CI → squash-merge.
#
#   scripts/ship.sh [--title "<pr title>"] [--body-file <path>] [--no-merge] [--draft]
#                   [--ci-timeout <seconds>]
#
# Idempotent: re-running after a CI failure reuses the open PR. The merge step
# is the only place a PR is merged in this repository (the Bash guard blocks
# `gh pr merge` and a direct call to the REST merge endpoint alike), so CI green
# and clean attribution are always enforced.
#
# Every GitHub call goes through `gh api`, i.e. REST v3. The `gh pr *` porcelain
# is deliberately avoided: it issues GraphQL queries, and the cloud build
# environment's proxy serves REST but answers GraphQL with HTTP 403 (ADR 0008).
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
command -v gh >/dev/null 2>&1 || fail "gh CLI is required (cloud environments install it in scripts/cloud-setup.sh)"

# --- REST helpers -----------------------------------------------------------
# gh expands {owner}/{repo} from the checkout's remote.
api() { gh api -H "Accept: application/vnd.github+json" "$@"; }

# json_obj KEY VALUE [KEY VALUE ...] — build a JSON object for `api --input -`.
# Values are strings; a key suffixed ":json" has its value parsed as JSON, so a
# body that happens to read "true" is never coerced into a boolean.
json_obj() {
  python3 - "$@" <<'PY'
import json, sys
args = sys.argv[1:]
obj = {}
for key, value in zip(args[::2], args[1::2]):
    if key.endswith(":json"):
        obj[key[:-5]] = json.loads(value)
    else:
        obj[key] = value
print(json.dumps(obj))
PY
}

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

OWNER="$(api "repos/{owner}/{repo}" --jq .owner.login 2>/dev/null)" \
  || fail "cannot reach the GitHub REST API. Check 'gh auth status' and that github.com is allowlisted."
PR_NUM="$(api -X GET "repos/{owner}/{repo}/pulls" -f head="$OWNER:$BRANCH" -f state=open \
            --jq '.[0].number // empty' 2>/dev/null || true)"
if [ -z "$PR_NUM" ]; then
  json_obj title "$TITLE" head "$BRANCH" base "$BASE" body "$(cat "$BODY_TMP")" \
           "draft:json" "$( [ -n "$DRAFT" ] && echo true || echo false )" \
    | api -X POST "repos/{owner}/{repo}/pulls" --input - >"$STATE_DIR/pr-create.log" 2>&1 \
    || { cat "$STATE_DIR/pr-create.log" >&2; fail "creating the pull request failed"; }
  PR_NUM="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])' <"$STATE_DIR/pr-create.log")" \
    || fail "pull request created but its number could not be read (see .claude/run/pr-create.log)"
  echo "ship: opened PR #$PR_NUM"
else
  json_obj title "$TITLE" body "$(cat "$BODY_TMP")" \
    | api -X PATCH "repos/{owner}/{repo}/pulls/$PR_NUM" --input - >/dev/null 2>&1 || true
  echo "ship: reusing PR #$PR_NUM"
fi
rm -f "$BODY_TMP"
PR_URL="$(api "repos/{owner}/{repo}/pulls/$PR_NUM" --jq .html_url)"

[ "$MERGE" = "1" ] || { echo "ship: PR ready (not merged): $PR_URL"; exit 0; }

# 6. Wait for CI. Checks can take a moment to register after the push.
#    Check runs are the GitHub Actions jobs; the combined-status endpoint covers
#    legacy commit statuses and reports total_count 0 (with state "pending") when
#    a repository uses none, so an empty status set is never read as unfinished.
HEAD_SHA="$(git rev-parse HEAD)"
echo "ship: waiting for checks on PR #$PR_NUM (timeout ${CI_TIMEOUT}s)"
START=$(date +%s)
while :; do
  ELAPSED=$(( $(date +%s) - START ))
  [ "$ELAPSED" -lt "$CI_TIMEOUT" ] || fail "CI did not finish within ${CI_TIMEOUT}s on PR #$PR_NUM ($PR_URL)"

  RUNS="$(api "repos/{owner}/{repo}/commits/$HEAD_SHA/check-runs?per_page=100" \
            --jq '.check_runs[] | "\(.status)\t\(.conclusion // "")\t\(.name)"' 2>/dev/null || true)"
  STATUS_LINE="$(api "repos/{owner}/{repo}/commits/$HEAD_SHA/status" \
                   --jq '"\(.total_count)\t\(.state)"' 2>/dev/null || printf '0\tsuccess')"
  STATUS_COUNT="${STATUS_LINE%%	*}"; STATUS_STATE="${STATUS_LINE##*	}"
  [ -n "$STATUS_COUNT" ] || STATUS_COUNT=0

  if [ -z "$RUNS" ] && [ "$STATUS_COUNT" = "0" ]; then
    [ "$ELAPSED" -lt 300 ] || fail "no CI checks registered on PR #$PR_NUM after 5 minutes. Is the workflow enabled? $PR_URL"
    sleep 15; continue
  fi

  # Fail fast on the first conclusive failure, as `gh pr checks --fail-fast` did.
  FAILED="$(printf '%s\n' "$RUNS" | awk -F'\t' '$1=="completed" && $2!="success" && $2!="neutral" && $2!="skipped" {print $3" ("$2")"}')"
  if [ -n "$FAILED" ] || { [ "$STATUS_COUNT" != "0" ] && [ "$STATUS_STATE" = "failure" ]; }; then
    printf '%s\n' "$RUNS" >"$STATE_DIR/ci.log"
    echo "ship: CI failed on PR #$PR_NUM ($PR_URL):" >&2
    [ -n "$FAILED" ] && printf '  %s\n' "$FAILED" >&2
    [ "$STATUS_STATE" = "failure" ] && echo "  combined commit status: failure" >&2
    echo "ship: inspect with: gh api repos/{owner}/{repo}/commits/$HEAD_SHA/check-runs --jq '.check_runs[]|select(.conclusion!=\"success\")|.html_url'" >&2
    exit 1
  fi

  PENDING=0
  [ -n "$RUNS" ] && PENDING="$(printf '%s\n' "$RUNS" | awk -F'\t' '$1!="completed"' | grep -c . || true)"
  if [ "$PENDING" = "0" ] && { [ "$STATUS_COUNT" = "0" ] || [ "$STATUS_STATE" = "success" ]; }; then
    break
  fi
  sleep 20
done
printf '%s\n' "$RUNS" >"$STATE_DIR/ci.log"
echo "ship: CI green"

# 7. Merge as the repository owner; the squash commit carries the PR author identity.
#    commit_title is the bare title: GitHub appends " (#N)" to a squash subject.
#
#    In a cloud session the write goes out under the Claude GitHub App's own
#    credentials, whatever `gh api user` reports, so GitHub authors the squash
#    commit `claude[bot]` — which attribution-check.sh rejects, leaving `main`
#    red and a tool identity in the history. Reads and the PR steps above are
#    fine; only the merge carries an identity. So the merge is refused here and
#    left to a human or a local checkout, which is what the repository's
#    "author is always the owner" rule requires.
if [ -n "${CLAUDE_CODE_REMOTE:-}" ] && [ "${INDICAI_ALLOW_BOT_MERGE:-}" != "1" ]; then
  echo "ship: PR #$PR_NUM is green and mergeable: $PR_URL"
  fail "refusing to merge from a cloud session: GitHub would author the squash commit 'claude[bot]',
      which scripts/attribution-check.sh rejects and which turns the attribution job on $BASE red.
      Merge it from the GitHub UI or a local checkout. Set INDICAI_ALLOW_BOT_MERGE=1 to override
      (and add the bot address to ALLOWED_AUTHOR_EMAILS first, or $BASE will go red)."
fi

MERGE_BODY="$(git log --reverse --format='- %s' "origin/$BASE..HEAD")"
json_obj merge_method squash commit_title "$TITLE" commit_message "$MERGE_BODY" \
  | api -X PUT "repos/{owner}/{repo}/pulls/$PR_NUM/merge" --input - >"$STATE_DIR/merge.log" 2>&1 \
  || { cat "$STATE_DIR/merge.log" >&2; fail "merge failed for PR #$PR_NUM ($PR_URL)"; }
echo "ship: merged PR #$PR_NUM into $BASE ($PR_URL)"
# Delete the head branch, as `gh pr merge --delete-branch` did. The session proxy
# permits no write to the git-refs path, so say what was left behind rather than
# swallowing the error: the merge has landed and a surviving branch is cosmetic.
if ! api -X DELETE "repos/{owner}/{repo}/git/refs/heads/$BRANCH" >/dev/null 2>&1; then
  echo "ship: could not delete origin/$BRANCH (branch deletion is not permitted here); delete it in the GitHub UI"
fi

# 8. Return to an up-to-date base branch and clear the prompt marker.
git fetch -q origin "$BASE"
git switch -q "$BASE" 2>/dev/null || git checkout -q "$BASE"
git reset -q --hard "origin/$BASE"
git branch -D "$BRANCH" >/dev/null 2>&1 || true
rm -f "$STATE_DIR/active-prompt" "$STATE_DIR/report.md" "$STATE_DIR"/gate-*.ok "$STATE_DIR/stop-blocks"
MERGED_SHA="$(git rev-parse --short HEAD)"
echo "ship: $BASE is now at $MERGED_SHA"
exit 0
