#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit): keep edited files lint-clean immediately
# so the definition-of-done gate rarely fails on formatting.
# Never blocks; problems are reported on stderr for the model to see.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
read_hook_input
FILE="$(json_get tool_input.file_path)"
[ -n "$FILE" ] && [ -f "$FILE" ] || exit 0
cd "$REPO_ROOT" || exit 0

case "$FILE" in
  *.py)
    if command -v uv >/dev/null 2>&1; then
      uv run --frozen ruff format "$FILE" >/dev/null 2>&1 || true
      # Only mechanical fixes (import order, pyupgrade); unused-import removal (F401) is
      # deliberately not auto-applied because a file is often written in several edits.
      uv run --frozen ruff check --fix --select I,UP --quiet "$FILE" >/dev/null 2>&1 || true
      uv run --frozen ruff check --quiet "$FILE" 2>&1 | tail -20 >&2 || true
    fi ;;
  *.json)
    python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$FILE" 2>&1 | tail -3 >&2 || true ;;
  *.jsonl)
    python3 - "$FILE" >&2 <<'PY' || true
import json, sys
bad = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for n, line in enumerate(fh, 1):
        if not line.strip():
            continue
        try:
            json.loads(line)
        except Exception as exc:
            bad += 1
            if bad <= 3:
                print(f"{sys.argv[1]}:{n}: invalid JSONL line: {exc}")
if bad:
    print(f"{sys.argv[1]}: {bad} invalid line(s)")
PY
    ;;
  *.yaml|*.yml)
    uv run --frozen python -c 'import sys,yaml; yaml.safe_load(open(sys.argv[1]))' "$FILE" 2>&1 | tail -3 >&2 || true ;;
  *.sh)
    bash -n "$FILE" 2>&1 | tail -3 >&2 || true ;;
  *.ts|*.tsx|*.js|*.jsx|*.css)
    # UI packages own their formatter; run it only when the package has one installed.
    PKG_DIR="$(cd "$(dirname "$FILE")" && (npm prefix 2>/dev/null || pwd))"
    if [ -x "$PKG_DIR/node_modules/.bin/prettier" ]; then
      "$PKG_DIR/node_modules/.bin/prettier" --log-level silent --write "$FILE" >/dev/null 2>&1 || true
    fi ;;
esac
exit 0
