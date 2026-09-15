#!/usr/bin/env bash
# Manage the local Docker Compose stack in the profiles the build actually needs.
#   scripts/stack.sh core     # postgres redis qdrant minio tei   (retrieval + integration tests)
#   scripts/stack.sh obs      # + langfuse, grafana, prometheus
#   scripts/stack.sh voice    # + livekit, livekit-sip
#   scripts/stack.sh sparse   # + bge-sparse sidecar (hybrid retrieval; memory heavy)
#   scripts/stack.sh all      # everything (= make up)
#   scripts/stack.sh status   # health table
#   scripts/stack.sh wait <service>...   # block until healthy (max 20 min)
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
[ -f .env.stack ] || python3 infra/bootstrap.py
COMPOSE="docker compose --env-file .env.stack"
CORE="postgres redis qdrant minio tei"
OBS="langfuse-postgres clickhouse langfuse langfuse-worker prometheus grafana"
VOICE="livekit livekit-sip"

up() { $COMPOSE up -d --build --wait --wait-timeout 1200 "$@"; }
case "${1:-status}" in
  core)   up $CORE ;;
  obs)    up $CORE $OBS ;;
  voice)  up $CORE $VOICE ;;
  sparse) $COMPOSE --profile retrieval up -d --build --wait --wait-timeout 1200 $CORE bge-sparse ;;
  all)    $COMPOSE --profile retrieval up -d --build --wait --wait-timeout 1200 ;;
  down)   $COMPOSE --profile retrieval down ;;
  status) $COMPOSE --profile retrieval ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}' ;;
  wait)   shift; $COMPOSE up -d --wait --wait-timeout 1200 "$@" ;;
  logs)   shift; $COMPOSE logs --tail 200 "$@" ;;
  *) echo "usage: scripts/stack.sh {core|obs|voice|sparse|all|down|status|wait <svc>|logs <svc>}" >&2; exit 2 ;;
esac
