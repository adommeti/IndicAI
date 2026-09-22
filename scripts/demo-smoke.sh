#!/usr/bin/env bash
# Prove the stack is usable, not merely up: every API answers, every console is served,
# the seeded data comes back through the same endpoints the consoles call, and the
# metrics are scrapeable. Exits non-zero on the first thing that fails, naming it.
#
#   make demo-smoke              # the checks
#   make demo-smoke ARGS=--chat  # also drive one helpdesk turn per language (spends money)
#   bash scripts/demo-smoke.sh --chat   # the same, without make
#
# Only python3 and curl are assumed: no jq, no uv beyond the one call that derives the
# seeded module id, so this runs the same way on a laptop and in a container.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

UC1=${UC1_URL:-http://127.0.0.1:8001}
UC2=${UC2_URL:-http://127.0.0.1:8002}
UC3=${UC3_URL:-http://127.0.0.1:8003}
PROM=${PROMETHEUS_URL:-http://127.0.0.1:9090}
CHAT=0
for arg in "$@"; do [ "$arg" = "--chat" ] && CHAT=1; done

failed=0
pass() { printf '  \033[32mok\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; failed=1; }

# get URL -> body on stdout, non-zero on a non-2xx.
get() { curl -sS --max-time 20 -f "$1" 2>/dev/null; }
# check LABEL URL PYTHON_EXPR: fetch, then evaluate EXPR over the parsed JSON as `j`.
check() {
  local label=$1 url=$2 expr=$3 body
  if ! body=$(get "$url"); then fail "$label: $url did not answer 2xx"; return; fi
  if python3 -c "import json,sys; j=json.load(sys.stdin); sys.exit(0 if ($expr) else 1)" <<<"$body" 2>/dev/null; then
    pass "$label"
  else
    fail "$label: $url answered, but not with what was expected: $(head -c 200 <<<"$body")"
  fi
}

echo "health"
for pair in "uc1 $UC1" "uc2 $UC2" "uc3 $UC3"; do
  set -- $pair
  check "$1 /health" "$2/health" "j.get('status') == 'ok'"
done

echo "metrics (the same endpoints prometheus scrapes)"
for pair in "uc1 $UC1" "uc2 $UC2" "uc3 $UC3"; do
  set -- $pair
  if get "$2/metrics" | grep -q '^# HELP'; then pass "$1 /metrics"; else fail "$1 /metrics has no HELP lines"; fi
done

echo "consoles (a bundle is served, not a 404)"
for pair in "uc1 $UC1/" "uc2 $UC2/" "uc3 $UC3/ui/"; do
  set -- $pair
  if get "$2" | grep -qi '<div id="root"\|<script'; then pass "$1 console at $2"; else fail "$1 console at $2 is not a bundle"; fi
done

echo "uc3: the dev-bypass identity, the seeded queue, and the exclusion that keeps it out of the metrics"
check "uc3 /me carries the reviewer role" "$UC3/me" "'compliance_reviewer' in j.get('roles', [])"
check "uc3 /flags has the seeded queue" "$UC3/flags" "len(j.get('flags', [])) >= 20"
check "uc3 /metrics/precision reports the demo rows it excluded" "$UC3/metrics/precision" "j.get('demo_excluded', {}).get('flags', 0) >= 20"
check "uc3 audit chain status names its three tables" "$UC3/audit/chain_status" \
  "len(j.get('tables', [])) == 3 and 'ok' in j and 'breaks' in j"

echo "uc2: the seeded module, through the review endpoint the console calls"
MODULE=$(uv run --frozen python -c "from training_localizer.demo_seed import module_uuid; print(module_uuid('sec-101'))" 2>/dev/null)
if [ -z "$MODULE" ]; then
  fail "could not derive the seeded uc2 module id (is uv available?)"
else
  check "uc2 review has 20 segments, 10 translated (hi-IN)" "$UC2/modules/$MODULE/review?language=hi-IN" \
    "len(j.get('segments', [])) == 20 and sum(1 for s in j['segments'] if s.get('translation')) == 10"
  check "uc2 review carries the quiz items" "$UC2/modules/$MODULE/review?language=hi-IN" "len(j.get('quiz_items', [])) == 10"
  check "uc2 status lists the seeded stages and says who wrote them" "$UC2/modules/$MODULE/status" \
    "j.get('languages', {}).get('hi-IN', {}).get('post_edit', {}).get('created_by') == ['demo-seed']"
fi

echo "uc1: the knowledge base and, with a key, one live turn"
check "uc1 /me answers with the bypass identity and its role" "$UC1/me" \
  "j.get('employee_id', '').endswith('@example.test') and 'governance' in j.get('roles', [])"
if [ "$CHAT" = 1 ]; then
  # Keys live in .env, which this script (run by you, not by an agent) may read.
  [ -f .env ] && { set -a; . ./.env; set +a; }
  if [ -n "${INDICAI_ANTHROPIC_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]; then
    for lang in hi-IN en-IN te-IN; do
      body=$(curl -sS --max-time 90 -f -H 'content-type: application/json' \
        -d "{\"utterance\":\"My laptop cannot connect to the office VPN since this morning\",\"language\":\"$lang\"}" \
        "$UC1/chat/turn" 2>/dev/null) \
        && pass "uc1 chat turn ($lang): session $(python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])' <<<"$body")" \
        || fail "uc1 chat turn ($lang) did not answer 2xx"
    done
  else
    echo "  skip uc1 chat: no Anthropic key in .env (INDICAI_ANTHROPIC_API_KEY)"
  fi
else
  echo "  skip uc1 chat: pass --chat to drive one turn per language (spends money)"
fi

echo "observability"
# infra/prometheus.yaml: job `apps` and job `workers`, each target labelled app=uc1|uc2|uc3.
check "prometheus has all three API targets up (job=apps)" "$PROM/api/v1/targets" \
  "{t['labels'].get('app') for t in j['data']['activeTargets'] if t['health']=='up' and t['labels'].get('job')=='apps'} >= {'uc1','uc2','uc3'}"

echo
if [ "$failed" = 0 ]; then
  cat <<EOF
Everything answered. Open:
  UC1 helpdesk chat        $UC1/
  UC2 reviewer console     $UC2/?module=$MODULE          (sec-101; languages hi-IN, te-IN, ta-IN)
  UC3 surveillance queue   $UC3/ui/
  Grafana                  http://127.0.0.1:3001         Prometheus $PROM
  Langfuse traces          http://127.0.0.1:3002
Live paths (spend money): make demo-smoke -- --chat | make demo-localize
EOF
else
  echo "Something did not answer. docs/DEMO.md has the troubleshooting table; 'make stack-status' shows container health." >&2
  exit 1
fi
