# P8-pipeline — Join ingestion to the detector and persist through the audit chain

<!-- Run: bash scripts/run-prompt.sh uc3 P8-pipeline -->
Read CLAUDE.md, `docs/prd-v2.md` Parts E4 and E5, `apps/comms_surveillance/ingest.py`,
`detector.py` and `audit.py`. UC3's halves were built separately and never joined.
`ingest.ingest_prefix` transcribes and stops at `transcript_segments`; `detector.analyse` is called
only by the eval runner and by tests; nothing outside `platform/tests/` constructs an `AnalysisRun`
or a `Flag`. Consequence: the reviewer queue, `GET /metrics/precision`,
`GET /metrics/false_negative_estimate`, the QA sample and the hash chain all operate over
permanently empty tables, and E4's user stories cannot be demonstrated from a real ingest.

1. Add a Celery task (`uc3.analyse`) that takes a `call_id`, loads its `transcript_segments`, runs
   `detector.analyse`, and writes the `analysis_runs` row and every `Flag` through `audit.append` —
   never a plain `session.add`, because those tables are append-only and hash-chained.
2. Chain it from the nightly sweep so a transcribed call is analysed in the same pass, and make it
   safe to re-run: analysing a call twice must not append a second run for the same
   `(call_id, policy_version, lexicon_version, prompt_version, theta)`. Choose the idempotency key
   deliberately and say why in the module docstring — re-analysis after a policy bump is a new run,
   a retry of a lost task is not.
3. Keep it inside the existing spend scope: `budget.session_scope(str(call_id))` already wraps
   transcription in `ingest.py`; analysis must be charged to the same call, and a test must fail if
   that `with` is removed.
4. Persist `model`, `prompt_version`, `policy_version`, `lexicon_version` and `theta` on the row, as
   uc3/P4 already computes them. `render_english` currently uses an unversioned inline prompt and
   swallows every exception — move it to `prompts/render_english.md`, hash it into `prompt_version`,
   and log the failure it swallows.
5. Prove the chain over real rows: extend `make audit` / the chain-verify path so a run that
   analysed calls produces a walk longer than zero, and assert the app role still cannot UPDATE or
   DELETE what it just wrote.

Acceptance: an integration test ingests a golden call, analyses it, and reads the resulting flags
back through `GET /flags`; `analysis_runs` and `flags` each gain rows; `audit.verify` walks them
clean; re-running the task appends nothing; removing the spend scope fails a test. All of this runs
in CI's integration job (postgres, redis, qdrant), which is where it must be proven.

## Execution notes
- No Docker daemon in a cloud session, so the integration tests are written here and proven in CI.
  Mocked-vendor unit tests must still pass in `make check`.
- Do not widen the `uc3_app` grant. If a write needs a privilege the role lacks, that is a finding
  for the report, not a migration — ADR 0016 explains why the chain's privileges are the control.
- This closes the BLOCKERS row "nothing persists an `analysis_runs` or a `flags` row outside tests"
  and makes uc3's retention, metrics and QA-sample arguments describe real data. Update those rows.
- Vendor cost: analysing the golden set live is uc3/P9's business, not this prompt's. Use mocked
  vendors here; one live call on one call id is enough to prove the wire, and print its estimate.
