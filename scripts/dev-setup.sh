#!/usr/bin/env bash
# One-time local developer setup (the cloud session does this automatically
# through .claude/hooks/session-start.sh). Safe to re-run.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/* .claude/hooks/*.sh scripts/*.sh
git config --local core.hooksPath .githooks
git config --local commit.gpgsign false
[ -f .env ] || cp .env.example .env
[ -f .env.stack ] || python3 infra/bootstrap.py
uv sync --frozen --all-packages
uv run pre-commit install >/dev/null 2>&1 || true
echo "dev-setup: done. Fill SARVAM_API_KEY / ANTHROPIC_API_KEY in .env, then: make stack-core && make check"
