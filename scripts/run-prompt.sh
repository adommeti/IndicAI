#!/usr/bin/env bash
# Start a build prompt: fresh branch off the base branch, prompt marker set,
# prompt text printed for the agent to execute.
#   scripts/run-prompt.sh <program|uc1|uc2|uc3> <P0|P1|...|P8-eval-refactor>
#   scripts/run-prompt.sh --status        # show plan status table
#   scripts/run-prompt.sh --abort         # drop the marker (branch is kept)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
source .claude/hooks/_lib.sh
BASE="$(base_branch)"
PLAN="docs/build/PROMPT-PLAN.md"

case "${1:-}" in
  --status) grep -E '^\| *(program|uc[123]) *\|' "$PLAN" 2>/dev/null || echo "no plan table found in $PLAN"; exit 0 ;;
  --abort)  rm -f "$STATE_DIR/active-prompt"; echo "run-prompt: marker cleared"; exit 0 ;;
  ""|--next)
    # First pending row whose prerequisites are all done, in plan order.
    NEXT="$(python3 - "$PLAN" <<'PY'
import re, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8"):
    m = re.match(r"^\| *(program|uc[123]) *\| *(\S+) *\| *(\w+) *\| *([^|]*?) *\|", line)
    if m:
        rows.append(m.groups())
done = {f"{g}/{p}" for g, p, s, _ in rows if s == "done"}
for g, p, s, needs in rows:
    if s != "pending":
        continue
    deps = [d.strip() for d in needs.replace("—", "").split(",") if d.strip()]
    if all(d in done for d in deps):
        print(f"{g} {p}")
        break
PY
)"
    [ -n "$NEXT" ] || { echo "run-prompt: no pending prompt with satisfied prerequisites" >&2; exit 2; }
    echo "run-prompt: next = $NEXT"
    exec bash "$0" $NEXT ;;
esac
GROUP="${1:-}"; PN="${2:-}"
FILE="prompts/$GROUP/$PN.md"
[ -f "$FILE" ] || { echo "run-prompt: prompt file not found: $FILE" >&2; ls prompts/*/ >&2; exit 2; }

if [ -f "$STATE_DIR/active-prompt" ] && [ "$(cat "$STATE_DIR/active-prompt")" != "$GROUP/$PN" ]; then
  echo "run-prompt: another prompt is active: $(cat "$STATE_DIR/active-prompt"). Ship it or run --abort first." >&2; exit 2
fi
[ -z "$(git status --porcelain)" ] || { echo "run-prompt: working tree is dirty; commit or stash first." >&2; exit 2; }

# Prerequisite check against the plan table: every row this prompt "needs" must be done.
NEEDS="$(grep -E "^\| *$GROUP *\| *$PN *\|" "$PLAN" 2>/dev/null | awk -F'|' '{gsub(/^ +| +$/,"",$5); print $5}')"
if [ -n "$NEEDS" ] && [ "$NEEDS" != "—" ] && [ "$NEEDS" != "-" ]; then
  for dep in $(printf '%s' "$NEEDS" | tr ',' ' '); do
    dg="${dep%%/*}"; dp="${dep##*/}"
    st="$(grep -E "^\| *$dg *\| *$dp *\|" "$PLAN" | awk -F'|' '{gsub(/^ +| +$/,"",$4); print $4}')"
    [ "$st" = "done" ] || { echo "run-prompt: prerequisite $dep is '$st', not done. Run it first (or override with INDICAI_SKIP_PREREQS=1)." >&2; [ "${INDICAI_SKIP_PREREQS:-}" = "1" ] || exit 2; }
  done
fi

git fetch -q origin "$BASE" || true
git switch -q "$BASE" 2>/dev/null || git checkout -q "$BASE"
git merge -q --ff-only "origin/$BASE" 2>/dev/null || true
SLUG="$(printf '%s' "$PN" | tr '[:upper:]' '[:lower:]')"
BRANCH="build/$GROUP-$SLUG"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git switch -q "$BRANCH"
else
  git switch -q -c "$BRANCH"
fi
printf '%s/%s\n' "$GROUP" "$PN" >"$STATE_DIR/active-prompt"
rm -f "$STATE_DIR/report.md" "$STATE_DIR/stop-blocks"

cat <<EOF
run-prompt: active=$GROUP/$PN branch=$BRANCH base=origin/$BASE@$(git rev-parse --short "origin/$BASE")
run-prompt: when done → write .claude/run/report.md, set status 'done' in $PLAN, commit, then: bash scripts/ship.sh
================================================================================
EOF
# Print the prompt body without the leading HTML comment / run line.
sed -E '/^<!--.*-->$/d' "$FILE"
