#!/bin/bash
# Cloud environment setup script.
# Paste the CONTENTS of this file into the "Setup script" field of the cloud
# environment used for this repository (claude.ai/code → environment settings).
# It runs as root, before Claude Code launches, on the first session of a new
# environment cache; its results (packages, pulled images, model weights) are
# snapshotted and reused by later sessions. Must finish in ~5 minutes and exit 0.
set -x
export DEBIAN_FRONTEND=noninteractive

# Locate the repository clone (cwd is not guaranteed).
REPO=""
for d in "$PWD" "$PWD"/IndicAI /home/*/IndicAI /root/IndicAI /workspace/IndicAI /workspaces/IndicAI; do
  [ -f "$d/docker-compose.yml" ] && [ -f "$d/pyproject.toml" ] && REPO="$d" && break
done
[ -n "$REPO" ] || REPO="$(find / -maxdepth 5 -name docker-compose.yml -path '*IndicAI*' 2>/dev/null | head -1 | xargs -r dirname)"

# System packages used by the adapters, evals, and media prompts.
(apt-get update -qq && apt-get install -y -qq ffmpeg shellcheck >/dev/null) || true

# GitHub CLI: scripts/ship.sh needs it for the PR, CI-wait and merge steps.
# Installed from the release tarball rather than apt so the version is pinned
# and no extra apt source is added; github.com and objects.githubusercontent.com
# are on the environment's network allowlist (RUNBOOK §1).
if ! command -v gh >/dev/null 2>&1; then
  GH_VERSION=2.63.2
  (curl -fsSL --max-time 120 -o /tmp/gh.tgz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && tar -xzf /tmp/gh.tgz -C /tmp \
    && install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_amd64") || true
fi

if [ -n "$REPO" ]; then
  cd "$REPO" || exit 0
  # Python workspace (cached in the snapshot).
  (uv sync --frozen --all-packages) || true
  # Stack credentials and image pulls/builds so `make stack-core` is fast in-session.
  [ -f .env.stack ] || python3 infra/bootstrap.py || true
  (docker compose --env-file .env.stack pull --ignore-buildable postgres redis minio langfuse-postgres clickhouse langfuse langfuse-worker prometheus grafana livekit livekit-sip 2>/dev/null &) || true
  (docker compose --env-file .env.stack build qdrant tei 2>/dev/null &) || true
  wait || true
  # Warm the bge-m3 weights into the tei-data volume so the first TEI start is not a 15-minute download.
  (timeout 240 docker compose --env-file .env.stack up -d tei && sleep 200 && docker compose --env-file .env.stack stop tei) || true
fi
exit 0
