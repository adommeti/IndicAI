# P7-produce — Give the UC2 production stage a trigger

<!-- Run: bash scripts/run-prompt.sh uc2 P7-produce -->
Read CLAUDE.md, `docs/prd-v2.md` Part D3 (US-1, US-3) and D4, and `apps/training_localizer/
pipeline.py`. `uc2/P4` built the whole production chain and nothing starts it: `uc2.produce`
(`pipeline.py:745`) has no API route, no CLI and no make target, so `captions_task`, `dub_task`,
`tts_summary_task` and `package_task` are unreachable, `production.py` (411 lines), `captions.py`
and the `SarvamDubbing` adapter are dead by reachability, and `GET /delivery/{module_id}` reads
`Artifact` rows that only `package_task` writes.

1. Add the trigger: a route on the approved-module path that dispatches `uc2.produce`, guarded by
   the reviewer/owner role the module's state requires (see `uc2/P6-security` if it has landed;
   otherwise use the existing dependency and note the coupling). Production must only start from an
   approved module — producing an unapproved LOCKED segment is the failure D3 exists to prevent.
2. Make it idempotent per `(module_id, language, stage, version)` as `.claude/rules/apps.md`
   requires, and safe to retry: a re-dispatch after a lost task must not produce a second artifact
   set or a second dubbing job (which costs Rs 40/min).
3. Surface state: the delivery endpoint and the reviewer UI must distinguish "not produced",
   "in progress" and "produced", rather than an empty artifact list meaning both of the first two.
4. **D3 US-1 is also unbuilt and is not in this prompt's scope unless it is cheap**: `POST /modules`
   takes a pre-parsed JSON segment list and a `video_uri` string, with no DOCX/MD/SRT parser and no
   media upload, so the training-owner persona cannot start a job without a developer. Record it in
   `docs/build/BLOCKERS.md` with what it would take, and say plainly in the report that US-1 is not
   satisfied.

Acceptance: an integration test approves a module, triggers production with mocked vendors, and
reads the resulting artifacts back through `GET /delivery/{module_id}`; re-dispatching produces no
duplicate artifact and no second dubbing job; an unapproved module is refused; the delivery response
distinguishes the three states.

## Execution notes
- Mocked vendors only. A real dub needs `*.blob.core.windows.net` on the network allowlist and
  `ffmpeg` installed, neither of which a cloud session has — that stays blocked and stays recorded.
  The code path is already tested against the recorded uc2/P0-spike responses; this prompt adds the
  trigger, not the vendor call.
- Integration tests run in CI (postgres, redis, qdrant). Unit tests must pass in `make check`.
- Keep the change minimal: this is a wiring prompt. Redesigning the module lifecycle is not in it.
