#!/usr/bin/env bash
# One command from a fresh clone to three consoles with data in them.
#
#   make demo
#
# Each step is idempotent, so re-running after a failure picks up where it stopped:
# credentials are generated once, images are rebuilt only when their inputs changed,
# migrations are no-ops at head, the knowledge base re-ingests in place, and both
# seeders skip what is already there. The first run is slow -- three image builds and,
# once, TEI downloading the bge-m3 weights (up to 15 minutes; docker-compose gives it
# that long) -- and every later run takes about a minute.
#
# What it does NOT do: spend money. The chat, the uc2 pipeline and uc3's analysis all
# call vendors; this leaves them to you (see docs/DEMO.md, "The live paths").
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)" >&2; exit 1; }; }

need docker "https://docs.docker.com/get-docker/"
need uv "https://docs.astral.sh/uv/"
need python3 "the Makefile's own helper interpreter"
docker info >/dev/null 2>&1 || { echo "the Docker daemon is not running or not reachable" >&2; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  cat >&2 <<'EOF'
No .env was present, so .env.example was copied into place. The stack will start and the
seeded data will render without any key. To exercise the live paths -- the helpdesk chat,
`make demo-localize`, uc3's real analysis -- put these in .env and re-run `make demo`:
    SARVAM_API_KEY=
    INDICAI_ANTHROPIC_API_KEY=
EOF
fi

step "stack credentials (.env.stack, generated once)"
python3 infra/bootstrap.py

step "images and services (first run: image builds, then TEI's one-time model download)"
make up

step "database migrations"
make migrate

step "uc1 knowledge base -> qdrant (through TEI; about a minute)"
make ingest-kb

step "uc2 and uc3 demonstration data"
make seed

step "smoke"
bash scripts/demo-smoke.sh
