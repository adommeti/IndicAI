#!/usr/bin/env bash
# Single entry point for "is this tree acceptable?".
#   --gate  : lint, typecheck, unit tests, offline evals, attribution (default)
#   --full  : gate + integration tests (needs the core stack) + audit
#   --quick : lint + typecheck only
# Exit non-zero on the first failing stage; prints a stage summary.
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || dirname "$0")/" || exit 1
MODE="${1:---gate}"
export LANGFUSE_TRACING_ENABLED="${LANGFUSE_TRACING_ENABLED:-false}"
export LIVE_API_TESTS=0
FAILED=""

stage() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then printf -- '--- %s: ok\n' "$name"; else printf -- '--- %s: FAILED\n' "$name"; FAILED="$FAILED $name"; return 1; fi
}

stage lint        uv run --frozen ruff check . || exit 1
stage format      uv run --frozen ruff format --check . || exit 1
stage typecheck   uv run --frozen mypy platform apps infra || exit 1
[ "$MODE" = "--quick" ] && { echo "quick checks passed"; exit 0; }

stage unit-tests  uv run --frozen pytest -m 'not slow and not integration' -q -p no:cacheprovider || exit 1
stage eval-uc1-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc1 || exit 1
stage eval-uc2-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc2 || exit 1
stage eval-uc3-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc3 || exit 1
stage attribution bash scripts/attribution-check.sh || exit 1
stage secrets     bash -c 'uv run --frozen detect-secrets-hook --baseline .secrets.baseline $(git ls-files)' || exit 1

if [ "$MODE" = "--full" ]; then
  stage integration uv run --frozen pytest -m integration -q -p no:cacheprovider || exit 1
  stage audit       uv run --frozen pip-audit --skip-editable || exit 1
fi

echo
echo "all checks passed ($MODE)"
