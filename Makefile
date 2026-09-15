UV ?= uv
COMPOSE = docker compose --env-file .env.stack
.PHONY: bootstrap up down logs lint typecheck test test-integration eval-uc1 eval-uc2 eval-uc3 ingest-kb voice-test migrate audit \
        check check-quick check-full stack-core stack-obs stack-voice stack-sparse stack-status stack-logs ship plan
bootstrap:
	python3 infra/bootstrap.py
up: bootstrap
	$(COMPOSE) up -d --build --wait --wait-timeout 1200
down:
	$(COMPOSE) --profile retrieval down
stack-core stack-obs stack-voice stack-sparse: bootstrap
	bash scripts/stack.sh $(@:stack-%=%)
stack-status:
	bash scripts/stack.sh status
stack-logs:
	bash scripts/stack.sh logs
check:
	bash scripts/checks.sh --gate
check-quick:
	bash scripts/checks.sh --quick
check-full:
	bash scripts/checks.sh --full
test-integration:
	$(UV) run pytest -m integration
ship:
	bash scripts/ship.sh
plan:
	bash scripts/run-prompt.sh --status
logs:
	$(COMPOSE) logs --tail 100 -f
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
typecheck:
	$(UV) run mypy platform apps infra
test:
	$(UV) run pytest -m 'not slow and not integration'
eval-uc1:
	$(UV) run python -m indic_platform.eval.runners.run_uc1 --baseline --retrieval --compare-translate
eval-uc2 eval-uc3:
	$(UV) run python -m indic_platform.eval.runners.run --app $(@:eval-%=%)
ingest-kb:
	$(UV) run python -m indic_platform.cli ingest-kb
voice-test:
	LIVE_API_TESTS=1 $(UV) run pytest -m slow -k sarvam
migrate:
	$(UV) run alembic upgrade head
audit:
	$(UV) run pip-audit --skip-editable
