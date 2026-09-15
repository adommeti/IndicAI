UV ?= uv
UC1_EVAL_ARGS ?=
# `make eval-uc2` stays offline and free, because CI runs it and
# .claude/rules/eval.md says offline CI uses the checked-in scaffold. The vendor
# runs are opt-in targets below.
UC2_EVAL_ARGS ?= --baseline
# The uc2/P2 pipeline under the B6 gates. Both extra flags are load-bearing:
# `--fidelity-source sut` points the judge at this pipeline rather than at
# uc2/P1's draft references, and `--pre-edit` scores the translate stage before
# post_edit, which is the adherence number that can fail (post_edit enforces
# exactly what the scorer tests, so the headline number cannot).
UC2_LIVE_ARGS ?= --translate training_localizer.eval_hook:full \
	--pre-edit training_localizer.eval_hook:translate_only \
	--fidelity-source sut --live
# Sarvam only: real terminology adherence without adapt or the judge, ~Rs 28.
UC2_SARVAM_ARGS ?= --translate training_localizer.eval_hook:translate_and_enforce \
	--pre-edit training_localizer.eval_hook:translate_only --baseline
COMPOSE = docker compose --env-file .env.stack
.PHONY: bootstrap up down logs lint typecheck test test-integration eval-uc1 eval-uc2 eval-uc2-live eval-uc2-sarvam eval-uc3 ingest-kb voice-test migrate audit \
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
	$(UV) run python -m indic_platform.eval.runners.run_uc1 --chat-only --decide helpdesk_agent.graph:decide $(UC1_EVAL_ARGS)
eval-uc2:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_EVAL_ARGS)
eval-uc2-live:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_LIVE_ARGS)
eval-uc2-sarvam:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_SARVAM_ARGS)
eval-uc3:
	$(UV) run python -m indic_platform.eval.runners.run --app uc3
ingest-kb:
	$(UV) run python -m indic_platform.cli ingest-kb
voice-test:
	LIVE_API_TESTS=1 $(UV) run pytest -m slow -k sarvam
migrate:
	$(UV) run alembic upgrade head
audit:
	$(UV) run pip-audit --skip-editable
