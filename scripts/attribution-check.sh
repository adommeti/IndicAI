#!/usr/bin/env bash
# Verify every commit in a range is authored by the repository owner and
# carries no tool-attribution trailers. Used by the Stop gate, pre-push,
# ship.sh and CI.
#   scripts/attribution-check.sh [<range>]     default: origin/<base>..HEAD
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
source .claude/hooks/_lib.sh

BASE="$(base_branch)"
RANGE="${1:-origin/$BASE..HEAD}"
if ! git rev-parse --verify --quiet "${RANGE%%..*}" >/dev/null 2>&1; then
  echo "attribution-check: range base '${RANGE%%..*}' not found; checking HEAD only"
  RANGE="HEAD~1..HEAD"
  git rev-parse --verify --quiet HEAD~1 >/dev/null 2>&1 || RANGE="HEAD"
fi

ALLOWED_EMAILS="${INDICAI_ALLOWED_EMAILS:-$ALLOWED_AUTHOR_EMAILS_DEFAULT}"
# The AUTHOR is the attribution that GitHub shows and counts, so it is checked strictly.
# The COMMITTER is mechanical — it becomes whoever ran `git am`, `git rebase` or the squash
# merge — so any human identity is accepted there, but tool identities never are.
TOOL_IDENTITY_RE='claude|anthropic|copilot|\[bot\]|noreply@anthropic'
STATUS=0
COMMITS="$(git rev-list "$RANGE" 2>/dev/null || true)"
[ -n "$COMMITS" ] || { echo "attribution-check: no commits in $RANGE"; exit 0; }

for sha in $COMMITS; do
  an="$(git log -1 --format='%an' "$sha")"; ae="$(git log -1 --format='%ae' "$sha")"
  cn="$(git log -1 --format='%cn' "$sha")"; ce="$(git log -1 --format='%ce' "$sha")"
  body="$(git log -1 --format='%B' "$sha")"
  short="$(git log -1 --format='%h %s' "$sha")"
  ok=1
  case ",$ALLOWED_EMAILS," in
    *",$ae,"*) ;;
    *) echo "FAIL $short: author '$an <$ae>' is not an allowed identity"
       echo "      allowed: $ALLOWED_EMAILS"
       echo "      a squash merge is authored by the merging GitHub account, so that"
       echo "      account's email must be in the ALLOWED_AUTHOR_EMAILS repo variable"; ok=0 ;;
  esac
  if printf '%s <%s>' "$cn" "$ce" | grep -Eiq "$TOOL_IDENTITY_RE"; then
    echo "FAIL $short: committer '$cn <$ce>' is a tool identity"
    echo "      fix: git -c user.name='$GIT_NAME' -c user.email='$GIT_EMAIL' commit --amend --reset-author  (or re-apply with that identity configured)"
    ok=0
  fi
  if printf '%s\n' "$body" | grep -Eiq "$FORBIDDEN_TRAILER_RE"; then
    echo "FAIL $short: message contains a tool-attribution line:"; printf '%s\n' "$body" | grep -Ei "$FORBIDDEN_TRAILER_RE" | sed 's/^/    /'; ok=0
  fi
  [ "$ok" = "1" ] || STATUS=1
done
[ "$STATUS" = "0" ] && echo "attribution-check: $(printf '%s\n' "$COMMITS" | wc -l | tr -d ' ') commit(s) in $RANGE clean"
exit $STATUS
