# P12-runtime — Make the three apps runnable: images, services, workers, metrics

<!-- Run: bash scripts/run-prompt.sh program P12-runtime -->
Read CLAUDE.md and `docs/prd-v2.md` Parts B7, B8 and E8. The programme has built three applications
and never packaged any of them. There is no Dockerfile for any app (`infra/sparse/Dockerfile` is the
only one in the repo), no app service in `docker-compose.yml`, no uvicorn entrypoint, **no Celery
worker or beat process anywhere** — 17 registered tasks and 4 beat schedules that never execute —
and nothing serves `/metrics`, so every counter in `platform/obs/metrics.py`, including the T9
budget alerts, increments into a registry nobody scrapes. `make up` starts dependencies, not the
product. `program/P10` assumes the apps can already start, so this runs first.

1. **Images.** One Dockerfile per app (or one parameterised image), built from the uv workspace with
   `uv sync --frozen`, non-root, with the app's UI built and served where the app owns one. Only
   `apps/training_localizer/api.py` currently mounts `StaticFiles`; uc1's and uc3's READMEs and
   `vite.config.ts` both claim their API serves `dist/` and neither does. Make the claim true or
   delete it.
2. **Services.** Compose services for the three APIs, plus a Celery worker and a beat for each app
   that has tasks, with the queues separated so a UC3 batch night cannot starve UC1's ticket
   retries. Add make targets. This is what finally makes retention, the nightly chain verify and
   ticket retries run at all.
3. **Metrics.** Mount `prometheus_client.make_asgi_app()` on each API and add the three targets to
   `infra/prometheus.yaml`. Then add the alert rules PRD E8 asks for — `infra/grafana/` has two
   dashboards and zero alert rules, and `audit.chain_verify` currently emits a break as a Langfuse
   span only. Add the missing uc3 dashboard.
4. **`BudgetExceeded` is unhandled everywhere** and propagates as a 500 or a task failure. Catch it
   at the API and task boundaries and return a 429 or a degraded response.
5. **Per-app spend caps.** B8 says caps are set at 2x the estimate *for each app*; the ledger is one
   process-wide counter with one set of limits, so a UC3 batch can consume UC1's headroom. Make the
   caps per app, keeping the 50/80/100% alert-once semantics.
6. Health endpoints report stale stages (`scaffold`, `P2`, `P6`). Make them report something true.

Acceptance: `make up` starts the three apps, their workers and their beats; each `/health` answers;
each `/metrics` is scraped by Prometheus and shows a non-zero counter after one request; a beat
schedule fires a task in a test window; `BudgetExceeded` returns 429 rather than 500; per-app caps
are enforced and tested; `docker compose config` and a build of every image pass in CI.

## Execution notes
- No Docker daemon in a cloud session: images and compose are authored here, `docker compose config`
  and the image builds are proven in CI, and "`make up` starts the product" is UNMEASURED with the
  exact command in the report. Say so plainly rather than implying it was run.
- Do not fold `program/P10`'s Azure work in. This prompt makes the product runnable anywhere; P10
  puts it on a VM.
- Keep `platform/` changes minimal — the metrics and budget modules exist and are tested; this is
  wiring and configuration, not a rewrite of either.
