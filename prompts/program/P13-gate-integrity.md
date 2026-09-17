# P13-gate-integrity — Make the CI gates test what their names claim

<!-- Run: bash scripts/run-prompt.sh program P13-gate-integrity -->
Read CLAUDE.md, `.claude/rules/eval.md` and `scripts/checks.sh`. The unit suite is strong — 1017
tests, zero skips — but the *eval* gate around it is not testing what it appears to. Three findings,
each verified:

- `platform/eval/golden/{uc1,uc2,uc3}/scaffold.jsonl` are **byte-identical** (md5
  `4b22bd27ddd239089f9890f6a2c9ec0b`, 8 items). `make eval-uc1-offline`, `-uc2`, `-uc3` run the same
  file three times under three names, scoring only `wrap_untrusted`, `redact` and evidence-exactness
  — no application code at all. `run.py`'s `GATES` dict holds *descriptive strings*
  (`"hit@3>=80%"`), not thresholds; nothing evaluates them.
- CI's `make eval-uc2` runs `--baseline` (untranslated source) and `make eval-uc3` runs `--baseline`
  (flags nothing). The only CI stages exercising real app logic are uc1's adversarial threshold
  under `--mocked-decisions` and uc3's Stage 0 lexicon.
- 13 Playwright e2e tests exist across the three UIs and run **nowhere**. All 181 vitest tests
  import from `src/lib/*.ts`; `@testing-library/react` is in no `package.json`, so **no React
  component is ever rendered** by an automated test.

1. Replace the three identical scaffolds with a real per-app offline gate: a small, checked-in
   subset per app that exercises that app's own decision path with recorded or mocked vendor
   responses, and scores the metrics the app owns. Where a metric genuinely needs live vendors or
   the stack, it stays `unmeasured` with a reason — that rule is not the problem here; the problem
   is three gates that look like three and are one.
2. Make `GATES` mean something or delete it. A dict of prose that no code reads is worse than no
   dict, because a reader takes it for a threshold table.
3. Run the Playwright suites somewhere — CI, against the in-memory harness each UI already has
   (`training_localizer.demo` is the model; uc3's drives a stub, which is worth saying in the
   report). Add component tests for the highest-risk components, starting with the ones that enforce
   LOCKED segments and role visibility.
4. `apps/training_localizer/ui/package.json` lints `src` only, while uc1 and uc3 lint `src tests
   e2e`. Make them consistent.
5. Fix the documentation that is actively false, because a reviewer will believe it: the root
   `README.md` still describes the repo as a P0 scaffold with "three FastAPI app skeletons" and
   claims SSO, retention, audit-chain governance and spend caps are unimplemented — all four exist;
   `apps/helpdesk_agent/README.md` says ticket actions are a stub that returns a null ticket id,
   which uc1/P4 replaced with real filing; `apps/comms_surveillance/README.md` presents
   ingest->detect->chain->queue as one pipeline; `infra/grafana/dashboards/uc2.json` tells the
   viewer that `adapter_calls` is written by every vendor call, and nothing writes that table at all.

Acceptance: the three offline eval targets score three different things and at least one real
decision path per app; no golden file is byte-identical to another; `GATES` is either enforced or
gone; the e2e suites run in CI; component tests exist for the LOCKED-segment and role-visibility
components; every documentation claim listed in step 5 is either true or removed, each verified by
reading the code it describes.

## Execution notes
- `adapter_calls` has no writer anywhere (`platform/db/models.py:39` has zero references outside its
  own definition) while two Grafana panels query it. Either give it the writer `runtime.record`
  implies, or delete the model, the panels and the retention sweep that prunes it — but do not leave
  a table that two dashboards claim is populated. Decide, and record the decision.
- This prompt may touch all three apps and the root README; that is deliberate and is the exception
  to the usual one-app scope. Keep the changes to gates, tests and documentation truth — no feature
  work.
- Nothing here needs vendor spend or Docker beyond what CI already provides.
