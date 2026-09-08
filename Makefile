UV ?= uv
COMPOSE = docker compose --env-file .env.stack
.PHONY: bootstrap up down logs lint typecheck test eval-uc1 eval-uc2 eval-uc3 ingest-kb voice-test migrate audit
bootstrap:
	python3 infra/bootstrap.py
up: bootstrap
	$(COMPOSE) up -d --build --wait --wait-timeout 1200
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs --tail 100 -f
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
typecheck:
	$(UV) run mypy platform apps infra
test:
	$(UV) run pytest -m 'not slow'
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
