#!/usr/bin/env bash
# Single entry point for "is this tree acceptable?".
#   --gate  : lint, typecheck, unit tests, offline evals, attribution, secrets, audit (default)
#   --full  : gate + migrations round-trip + integration tests (needs the core stack)
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

# `ticketing` is deselected alongside `integration`: those tests need the Zammad
# compose profile, which neither this gate nor CI provisions. Registering the
# marker without deselecting it here would run them against nothing. `voice` is
# deselected for the same reason -- it needs LiveKit and paid vendor calls, and
# `make voice-test` is its only runner.
stage unit-tests  uv run --frozen pytest -m 'not slow and not integration and not ticketing and not voice' -q -p no:cacheprovider || exit 1
stage eval-uc1-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc1 || exit 1
stage eval-uc2-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc2 || exit 1
stage eval-uc3-offline uv run --frozen python -m indic_platform.eval.runners.run --app uc3 || exit 1
# UI packages join the gate once they exist (.claude/rules/ui.md). Skipped when
# node_modules is absent so a Python-only checkout still passes `make check`;
# CI installs them, so a lint error there cannot slip through.
for ui in apps/*/ui; do
  [ -f "$ui/package.json" ] || continue
  if [ -d "$ui/node_modules" ]; then
    stage "ui:$(basename "$(dirname "$ui")"):lint" npm --prefix "$ui" run --silent lint || exit 1
    stage "ui:$(basename "$(dirname "$ui")"):typecheck" npm --prefix "$ui" run --silent typecheck || exit 1
    stage "ui:$(basename "$(dirname "$ui")"):test" npm --prefix "$ui" run --silent test || exit 1
  else
    printf '\n=== ui:%s ===\n--- skipped: run `npm --prefix %s install` to include it\n' \
      "$(basename "$(dirname "$ui")")" "$ui"
  fi
done

stage attribution bash scripts/attribution-check.sh || exit 1
stage secrets     bash -c 'uv run --frozen detect-secrets-hook --baseline .secrets.baseline $(git ls-files)' || exit 1
# The hooks themselves are enforced in CI (`pre-commit run --all-files`), not here:
# every hook's underlying tool already runs above as its own stage, and executing the
# hooks locally would download a hook environment per repo on first use and make this
# gate need the network for work it has already done. What is worth catching locally is
# a config that no longer parses or names a hook id that does not exist -- that is this
# stage, and it is offline and instant.
stage pre-commit-config uv run --frozen pre-commit validate-config .pre-commit-config.yaml || exit 1
# Moved out of --full and into the gate (uc1/P7). An advisory that fails CI has to fail
# `make check` too: while this ran only under --full, a finding that was red in CI was
# green locally, and the divergence was found by the PR rather than by the developer.
# ~8s. Ignores are justified one by one, with their reachability argument, in
# docs/security/audit-exceptions.md; keep this list in step with CI's and with the
# pip-audit hook in .pre-commit-config.yaml. An id here without an entry there is a
# silenced finding, not an accepted one. Needs network: pip-audit queries the advisory
# database, so an offline checkout fails this stage rather than skipping it quietly.
stage audit       uv run --frozen pip-audit --skip-editable \
  --ignore-vuln PYSEC-2026-3740 || exit 1

if [ "$MODE" = "--full" ]; then
  # Needs the stack: `alembic check` is the only thing that catches a model whose
  # column type has drifted from its shipped migration, and CI runs it too.
  stage migrations  bash -c 'uv run --frozen alembic upgrade head && uv run --frozen alembic check' || exit 1
  stage integration uv run --frozen pytest -m integration -q -p no:cacheprovider || exit 1
fi

echo
echo "all checks passed ($MODE)"
