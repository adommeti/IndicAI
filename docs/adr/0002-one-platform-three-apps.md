# ADR 0002 — One shared platform package, three thin apps, one stack

- **Status:** Accepted
- **Date:** 2026-09-15
- **Owner:** `program/P0`
- **Relates to:** ADR 0001, ADR 0005, ADR 0007, ADR 0008

## Context

`docs/prd-v2.md` B1 and B7 describe three use cases — helpdesk, training localization,
communications surveillance — that share almost everything that is hard: Sarvam and Anthropic
call handling, rate limiting, retries, circuit breaking, cost accounting, prompt-injection
hardening, redaction, evaluation, and traces. They share almost nothing that is interesting:
their workflows, prompts and UIs.

Three standalone services would have meant three copies of the adapter layer, three redaction
policies drifting apart, and three eval harnesses that cannot be compared. One monolith would
have coupled a compliance-sensitive workload to a productivity one, which ADR 0006 rejects.

## Decision

One shared Python package plus three thin FastAPI apps on one Docker Compose stack.

- The package lives at `platform/` and is **imported as `indic_platform`**. The directory name
  is not the import name, because `platform` shadows a Python standard-library module.
- `platform/adapters/` is the **only** place a vendor SDK is imported. Apps call
  `indic_platform.adapters.*`; they never import `sarvamai`, `anthropic`, or `httpx` against a
  vendor endpoint. Contracts are Protocols in `platform/adapters/base.py`, so a vendor swap is a
  one-file change.
- Everything cross-cutting — security hardening, redaction, config, observability, pricing,
  the eval harness and golden sets, the database models and migrations — lives in the package
  and is built once.
- Apps own their workflow, prompts, data model additions and UI, and nothing else.
- One `docker-compose.yml` with profiles (`core`, `obs`, `voice`, `sparse`) serves all three
  apps; there is no per-app stack. See ADR 0008 for the profiles and their memory budget.

## Consequences

- **Positive:** the injection hardening, the redactor, the cost model and the Langfuse span
  shape are written once and inherited. A fix lands for all three apps at the same time.
- **Positive:** evaluation is comparable across apps because the harness and report schema are
  shared; `program/P8-eval-refactor` can consolidate them because they already share a home.
- **Positive:** the vendor boundary is auditable by reading one directory.
- **Negative:** the three apps are coupled through shared files — `pyproject.toml` testpaths,
  the `Makefile`, alembic revisions. Parallel prompt branches collide there; `scripts/ship.sh`
  rebases, and two alembic heads need a merge revision (`.claude/rules/migrations.md`).
- **Negative:** a change to a Protocol signature is a breaking change for every app at once,
  which is why the adapter rule requires updating every implementation, every call site, the
  README interface table and a mocked-HTTP test in the same change.
- **Negative:** one stack means one memory budget. Running everything at once does not fit the
  16 GB cloud environment, hence the profiles.
- **Follow-up required:** the vendor-boundary rule is enforced by review and by
  `.claude/rules/adapters.md`, not by a test. `apps/` is clean today (no `sarvamai` or
  `anthropic` import outside `platform/adapters/`), but nothing fails the gate if that changes.
  An import-boundary check belongs in `program/P8-eval-refactor` or `program/P11`.

## Evidence

- `platform/pyproject.toml:16-17` — `packages = ["indic_platform", …]` with
  `package-dir = { indic_platform = "." }`: the directory/import-name split.
- `platform/adapters/base.py` — the Protocols (STT, TTS, Translate, Dubbing, LLM, VectorStore);
  the interface table in `README.md` restates them.
- `platform/adapters/runtime.py` — the single path every vendor call takes: shared token bucket,
  bounded retries, circuit breaker, timeouts, cost spans from `platform/config/pricing.yaml`.
- `platform/security/harden.py`, `platform/security/redact.py`, `platform/obs/langfuse.py` —
  shared controls; `platform/eval/` — shared harness and golden sets.
- `.claude/rules/adapters.md` — the boundary stated as a rule for the coding agent.
- `docker-compose.yml` with `scripts/stack.sh:14-16` — one stack, four profiles.
- `platform/tests/test_adapters.py` — adapter contracts exercised against mocked SDK clients;
  the only place `sarvamai` / `anthropic` appear outside `platform/adapters/`.
