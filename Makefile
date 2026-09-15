UV ?= uv
UC1_EVAL_ARGS ?=
# `make eval-uc2` stays offline and free, because CI runs it and
# .claude/rules/eval.md says offline CI uses the checked-in scaffold. The vendor
# runs are opt-in targets below.
UC2_EVAL_ARGS ?= --baseline
UC3_EVAL_ARGS ?= --baseline
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
# The `ticketing` profile (Zammad, uc1). Named explicitly because `up` with only
# a --profile flag would also start every unprofiled service.
ZAMMAD = zammad-postgresql zammad-redis zammad-memcached zammad-init \
	zammad-railsserver zammad-nginx zammad-scheduler zammad-websocket
.PHONY: bootstrap up down logs lint typecheck test test-integration eval-uc1 eval-uc2 eval-uc2-live eval-uc2-sarvam eval-uc3 eval-uc3-lexicon eval-uc3-full eval-uc3-diarize ingest-golden-audio ingest-kb voice-test migrate audit \
        check check-quick check-full test-ticketing stack-core stack-obs stack-voice stack-sparse stack-ticketing stack-status stack-logs ship plan
bootstrap:
	python3 infra/bootstrap.py
up: bootstrap
	$(COMPOSE) up -d --build --wait --wait-timeout 1200
down:
	$(COMPOSE) --profile retrieval --profile ticketing down
stack-core stack-obs stack-voice stack-sparse: bootstrap
	bash scripts/stack.sh $(@:stack-%=%)
# Zammad for uc1 ticket filing; see apps/helpdesk_agent/README.md. Eight extra
# containers (four Rails, plus its own Postgres/Redis/memcached), so it is opt-in
# and not part of stack-core. zammad-init runs migrations once and exits, so it
# is not waited on; the wait is on the two long-running services.
stack-ticketing: bootstrap
	$(COMPOSE) --profile ticketing up -d $(ZAMMAD)
	$(COMPOSE) --profile ticketing up -d --wait --wait-timeout 900 zammad-railsserver zammad-nginx
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
# Zammad-dependent tests. They are deselected from `make check` and from CI,
# which provision no Zammad -- so this target is the only place they run, and a
# marker with no runner is a test that never executes anywhere. Bring the
# profile up first: make stack-ticketing && python -m helpdesk_agent.zammad_seed
test-ticketing:
	$(UV) run pytest -m ticketing
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
	$(UV) run pytest -m 'not slow and not integration and not ticketing and not voice'
eval-uc1:
	$(UV) run python -m indic_platform.eval.runners.run_uc1 --chat-only --decide helpdesk_agent.graph:decide $(UC1_EVAL_ARGS)
eval-uc2:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_EVAL_ARGS)
eval-uc2-live:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_LIVE_ARGS)
eval-uc2-sarvam:
	$(UV) run python -m indic_platform.eval.runners.run_uc2 $(UC2_SARVAM_ARGS)
# uc3: the free baseline (flags nothing) is what CI runs. --diarize measures
# speaker attribution against live Saaras on the 20 audio items; it prints the
# estimate (about Rs 4) and refuses to spend without LIVE_API_TESTS=1, so run it
# as: LIVE_API_TESTS=1 make eval-uc3-diarize
eval-uc3:
	$(UV) run python -m indic_platform.eval.runners.run_uc3 $(UC3_EVAL_ARGS)
# Stage 0 only: the lexicon's own recall floor, free and offline.
eval-uc3-lexicon:
	$(UV) run python -m indic_platform.eval.runners.run_uc3 --detect comms_surveillance.stage0:detect --baseline
# The full three-stage detector against live Claude. Prints the estimate and
# refuses to spend without LIVE_API_TESTS=1: about $1 for the 200-item set.
eval-uc3-full:
	LIVE_API_TESTS=1 $(UV) run python -m indic_platform.eval.runners.run_uc3 --detect comms_surveillance.detector:detect --strict
eval-uc3-diarize:
	$(UV) run python -m indic_platform.eval.runners.run_uc3 --diarize --baseline
# Pushes the 20 golden WAVs through the real pipeline: MinIO, Postgres and live
# Saaras diarized STT. Needs `make stack-core`, `make migrate` and SARVAM_API_KEY.
ingest-golden-audio:
	$(UV) run python -m comms_surveillance.ingest
ingest-kb:
	$(UV) run python -m indic_platform.cli ingest-kb
# The uc1/P5 voice latency gate: joins the LiveKit room the pipeline is serving
# as a fake participant, plays 30 golden WAVs and measures time-to-first-audio
# from LiveKit track events. Needs Docker (`make stack-voice`, plus the core
# stack behind the decision stage), SARVAM_API_KEY and ANTHROPIC_API_KEY -- and
# it COSTS MONEY: 30 live turns of Saaras STT, Bulbul TTS and Claude.
# VOICE_TEST_GREETING must point at the recorded consent notice: a session
# refuses to start without one and this repo ships none (docs/build/BLOCKERS.md).
# `voice` is its own marker, deselected wherever `integration` and `ticketing`
# are: CI provisions no LiveKit and its integration job fails on any skip, so
# this target is the only place these tests run. Without the stack or the keys
# the test skips -- a skipped run measured nothing and is UNMEASURED, not a
# pass; VOICE_TEST_REQUIRE=1 turns those skips into failures.
voice-test:
	LIVE_API_TESTS=1 $(UV) run pytest -m voice -ra -s
migrate:
	$(UV) run alembic upgrade head
audit:
	$(UV) run pip-audit --skip-editable --ignore-vuln PYSEC-2026-3740
