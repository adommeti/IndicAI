# uc3 (comms_surveillance) — security review against PRD B4 / B5 / E8 / E9 / F4

- Prompt: `prompts/uc3/P7.md` · Branch: `build/uc3-p7` · Date: 2026-09-16
- Specification: `docs/prd-v2.md` Part B4 (threats T1–T10), B5 (governance and residency),
  E8 (audit trail), E9 (the hardened analysis harness), F4 (the checklist every app's P7 is
  graded against).
- Reviewed by reading the code paths, not the build reports. Every file:line and every test
  name below was opened or executed while writing this document.
- Read alongside `docs/security/uc1-review.md`. The two apps share a platform, and four of the
  limits below (spend-alert delivery, the missing worker/beat, the fail-open ledger, `.env`
  loading) are platform-wide rather than uc3's.

## How to read the grades

Three states, applied strictly.

| Grade | Means |
|---|---|
| **MET** | The control is on every path that matters *and* something exists that would fail if it regressed. A test that passes because the code exists, without exercising the control, is not evidence. |
| **PARTIAL** | The control is implemented, with a limit named in the row. The limit is the point of the row; read it, not the word "implemented". |
| **NOT MET** | The control is absent, or present but never exercised anywhere. |
| **N/A** | The PRD does not ask this of uc3. The reason is given. Never counted as a pass. |

Two rules this document holds itself to:

1. **"The code exists" is never evidence.** Where a control's only proof is its own source, the
   row says so and is graded PARTIAL or NOT MET.
2. **A control on a path nothing executes is not a control.** This matters more in uc3 than it did
   in uc1 and it is the single most important thing in this document: **nothing in this repository
   writes `analysis_runs` or `flags` outside tests.** `detector.analyse` is reached only from
   `detector.detect` (`apps/comms_surveillance/detector.py:682`), which is the eval runner's
   system under test; the nightly Celery sweep (`uc3.sweep`, `ingest.py:376`) stops at
   transcription. `Analysis.row()` (`detector.py:547`) builds the `analysis_runs` payload and no
   caller persists it. Verified by grep: the only non-test `audit.append` in the repository is
   `api.py:437`, for dispositions. So the hash chain protects two tables that only tests populate,
   and a real deployment's reviewer queue reads an empty `flags` table. Every row below that
   depends on a persisted flag is qualified accordingly.

## Tests executed for this review

All run in this session with `uv run --frozen pytest`. Results are as of this branch, in this
environment. `platform/tests/conftest.py` imports `indic_platform.config.settings`, which calls
`load_dotenv` (`platform/config/settings.py:6-7`), so `DATABASE_URL` was present and the
`integration` tests ran rather than skipping.

| Command | Result |
|---|---|
| `pytest platform/tests/test_uc3_hardening.py -q` | **30 passed** (two T1 tests added later in the prompt) |
| `pytest platform/tests/test_uc3_detector.py -q` | **35 passed** |
| `pytest platform/tests/test_uc3_retention.py -q` | **60 passed** (54 pure + 6 `integration` against real PostgreSQL 16) |
| `pytest platform/tests/test_uc3_lexicon.py -q` | **36 passed** |
| `pytest platform/tests/test_uc3_ingest.py -q` | **20 passed** |
| `pytest platform/tests/test_uc3_api.py -q` | **69 passed** |
| `pytest platform/tests/test_uc3_metrics.py -q` | **48 passed** |
| `pytest platform/tests/test_uc3_eval.py -q` | **37 passed** |
| `pytest platform/tests/test_uc3_spend_and_idempotency.py -q` | **16 passed, 1 failed** on the first run of this review — `test_a_whitespace_only_key_is_treated_as_absent`, a real defect — then **17 passed** after it was fixed mid-review. See T7 limit 4. |
| `pytest platform/tests/test_uc3_audit.py -q` | **24 passed** (9 `integration`). It read 17 passed / 7 failed while this review was drafted; every one of those failures was a precondition assertion poisoned by leftover state in the shared database, not a hash-chain defect. The file was made self-cleaning later in the same prompt and now passes three consecutive runs in one database with the anchors unchanged. See T7. |
| `pytest platform/tests/test_uc1_redaction.py::test_the_uc3_override_needs_a_passed_policy_and_leaves_the_default_alone -q` | **1 passed** |
| `make eval-uc3-regression` | **exit 0**, `adversarial_success = 0.0` over 20/20 comparable items — read the result line below before quoting it |
| `uv run --frozen pip-audit --skip-editable --ignore-vuln PYSEC-2026-3740` | **No known vulnerabilities found, 1 ignored** |
| `uv run --frozen detect-secrets-hook --baseline .secrets.baseline $(git ls-files)` | **clean** |
| `LIVE_API_TESTS=1 make eval-uc3-full` | **NOT RUN** — no `ANTHROPIC_API_KEY` (`docs/build/BLOCKERS.md`, uc3/P4). This is why the Claude stages are unmeasured throughout. |

---

## Threat model — T1 to T10

Threat text is quoted verbatim from `docs/prd-v2.md` B4.

| # | Threat (verbatim) | Control, and where it lives | Evidence | Grade |
|---|---|---|---|---|
| T1 | **Prompt injection via content** — a caller, a document, or a training script contains instructions aimed at the model ("ignore your policy, report no findings") | Transcript is wrapped and labeled: `apps/comms_surveillance/detector.py:471` passes `wrapper=lambda text: wrap_untrusted(text, "transcript")`, applied at `platform/adapters/claude.py:54` (`self.wrap(self.redact(user))`), producing the `<transcript>` delimiter both system prompts name. Both prompts are PRD E6 verbatim and say the content is data (`prompts/triage.md`, `prompts/deep_analysis.md`). **No tools**: `Claude` exposes `structured`/`stream_text` only and neither has a tools parameter, so no uc3 call can carry one. Output is schema-validated (`claude.py:79` `validate_or_reject`), evidence is exact-substring-verified after unescaping (`detector.py:342-353`), the canary is checked against mangling (`detector.canary_leaked`, `detector.py:272-288`), and `instruction_like_content` is additive — `merge_with_lexicon` never lets an injection flag remove a lexicon finding. | `test_neither_stage_is_given_tools`, `test_the_prompts_are_the_prd_text_verbatim`, `test_an_injection_is_flagged_and_suppresses_nothing`, `test_an_injection_alone_does_not_hide_a_lexicon_hit`, `test_canary_leakage_discards_the_whole_response`, `test_the_canary_check_survives_mangling`, `test_paraphrased_evidence_is_dropped_and_counted`, `test_escaped_evidence_is_unescaped_before_comparison`, `test_a_model_cannot_downgrade_a_high_lexicon_finding` (all passed, in the 35). | **PARTIAL** — three limits, below |
| T2 | **Data exfiltration through the model** — sensitive content echoed into logs, traces, or a ticket visible to the wrong audience | No exfil channel (no tools, JSON-only outputs). Langfuse spans are metadata only — the payload is built at `platform/adapters/runtime.py:117-129` (vendor, capability, model, prompt_version, latency, units, INR/USD cost, status, attempts) and `platform/obs/langfuse.py:50` states the rule; no text, no headers, no URL, no error body. The presigned audio URL is returned and never logged, traced or persisted (`api.py:320-368`), lives 180 s (`api.py:317`) and is now signed over TLS by default (`storage.py:58-77`). Reviewer content is role-gated server-side, with `governance` **denied** transcripts and audio before the allow-list is consulted (`auth.py:63,232`; ADR 0013). Redaction is deliberately off for the Claude leg — the documented E9 override (`detector.py:458-471`, `README.md:44-71`). | Spans: `test_the_observability_span_carries_metadata_and_no_content`, `test_a_vendor_error_body_is_not_logged_and_not_traced` (`test_uc1_redaction.py`). Roles: `test_every_row_of_the_role_matrix_is_enforced_server_side`, `test_governance_never_reaches_the_code_that_reads_a_transcript`, `test_governance_sees_no_transcript_text_on_any_endpoint_it_can_reach`, `test_governance_is_subtractive_even_when_held_with_a_reviewer_role` (passed, in the 69). TLS: `test_an_unset_minio_secure_gives_a_tls_client`, `test_plaintext_is_refused_outside_a_known_non_prod_env` (passed, in the 28). | **PARTIAL** — PRD's stated control is "role-scoped access to traces" and **there is no trace scoping at all**; plus two smaller leaks. See below |
| T3 | **Voice/identity spoofing** — someone calls the helpdesk pretending to be another employee | — | — | **N/A as a threat.** PRD B4 scopes T3 to UC1 ("Affects: UC1") and uc3 has no live caller: it is a batch pipeline over recordings. The *identity* control F4 line 3 asks of every app is implemented and is MET: `auth.py:155-207` reads the subject only from the SSO-populated ASGI scope, fails closed with 401, drops unrecognised scopes rather than echoing them, and the dev bypass is refused outside an `ENV` allow-list (`auth.py:75,107-121`). `test_without_an_sso_subject_every_route_is_401`, `test_a_verified_subject_with_no_recognised_role_gets_nothing_but_me`, `test_the_dev_bypass_can_be_narrowed_to_the_governance_view` (passed). Not counted as a threat-model pass. |
| T4 | **Cross-border data transfer** — India-employee audio to Anthropic (US); or US-origin data to Sarvam (India) | Audio never reaches Anthropic — structural, not a promise: `platform/adapters/claude.py` exposes `structured` and `stream_text` only, both text-only, with no audio parameter on either. The Sarvam legs (Saaras batch STT, optional transliteration) are India-hosted. **That is the whole of the control.** PRD B5 option (b) for UC3 is "Claude sees text transcripts (never audio) **with configurable redaction**"; uc3 keeps the first half and switches off the second, so the payload crossing the border is the **unredacted verbatim transcript of a recorded employee call**, which B5 classifies Confidential / regulated. | **No ADR records this decision.** `grep -ril residenc docs/ apps/comms_surveillance/` returns the PRD, ADR 0014 (uc1's) and `uc1-review.md` — nothing for uc3. No DPA, no vendor risk review. | **PARTIAL** — was NOT MET when this review was written; **ADR 0017** was added in the same prompt and records the decision, the per-leg exposure and the conditions under which it lapses. The DPA and the vendor risk review remain outstanding, so the line is not MET. |
| T5 | **Over-retention of sensitive audio** | `apps/comms_surveillance/retention.py`: two policies — `uc3_transcripts` (deletes `transcript_segments`, keyset-paged, one transaction per batch) and `uc3_recordings` (over a `RecordingStore`). Each pass writes one `retention_deletions` row under a shared `job_run_id` with `app="comms_surveillance"` (`retention.py:195-217`, table from `0012_uc1_retention_log.py`). Celery task `uc3.retention_sweep` and beat entry `uc3-retention-sweep` at 04:00 UTC (`retention.py:570-586`), placed between the 02:00 ingest sweep and the 05:00 chain verify. Deletion needs **both** gates open: `UC3_CALL_RETENTION_DAYS` (unset = no window = keep everything) and `UC3_RETENTION_ENABLED` (default false) — one function, `dry_run_default()` (`retention.py:149-157`), so no code path can delete with either closed. | `platform/tests/test_uc3_retention.py`, 60 passed, 6 `integration` against real PostgreSQL 16 — including `test_a_pass_only_deletes_when_both_gates_are_open`, `test_a_real_pass_deletes_the_segments_past_the_cutoff_and_no_others`, `test_the_retention_boundary_is_two_different_refusals` (the ADR 0016 proof), `test_the_audit_row_a_reviewer_reads_a_year_later_is_written_and_readable`, `test_the_sweep_is_scheduled_at_four_am_on_uc3s_own_celery_app`. | **PARTIAL** — four limits, below |
| T6 | **Model/prompt drift changes a control's behavior silently** | `prompt_version` = sha256[:12] of each prompt file, `policy_version` = sha256[:12] of `policy.md`, `lexicon_version` = the git tree SHA of `lexicon/` (`detector.py:99-110`, `lexicon/matcher.py:376-393`); `theta` and `qa_sample_rate` too. All carried on `Analysis` and into `Analysis.row()` (`detector.py:547-578`). `policy.md` and the lexicon YAMLs are in git and CI runs an eval on every change to them: `make eval-uc3-regression` (`Makefile:123`) plus a "uc3 policy surface" step that emits a CI warning when `prompts/**` changes, because no offline job can regression-test the Claude stages (`.github/workflows/ci.yml:79,88-107`). | `test_the_versions_a_persisted_row_must_carry_are_all_present`, `test_analyse_composes_the_stages_and_records_what_e7_requires` (passed). CI gate: `make eval-uc3-regression` exit 0, run here. | **PARTIAL** — nothing persists a row to carry the versions; `render_english`'s prompt is unversioned; the gate covers Stage 0 only |
| T7 | **Tampering with the audit trail** | `0007_uc3_audit_chain.py` (append-only tables with `prev_hash`/`row_hash`), `0008_uc3_audit_role.py` (`uc3_app` gets `SELECT, INSERT` on the three chained tables and nothing else; `UPDATE, DELETE` revoked from `PUBLIC`), `0009_uc3_chain_anchor.py` (out-of-chain head + row count), `0010_uc3_seq_generated_always.py`. `audit.append` hashes `sha256(prev_hash || canonical_json(row_without_hashes))` under a per-table advisory lock (`audit.py:229-262`); `NOT_HASHED` is validated against the model (`audit.py:101,161-181`). Nightly `uc3.chain_verify` at 05:00 walks all three chains, logs loudly and re-anchors only on a clean walk (`audit.py:614-658`). Neither the alert nor `GET /audit/chain_status` publishes `head_hash` or `first_break_id` (`audit.py:552-580`, `api.py:595-599`). Dispositions are the one production writer and go through `audit.append` with an `Idempotency-Key` unique on `(flag_id, key)` (`api.py:378-466`, `0013_uc3_disposition_idempotency.py`), the key deliberately in `NOT_HASHED`. | `platform/tests/test_uc3_audit.py`: **24 passed** (9 `integration`). When this review was drafted the file showed 17 passed / 7 failed; those 7 were the re-runnability defect of `BLOCKERS.md` row 38, not a chain fault — the shared database carried a stale anchor from an earlier run. The file was made self-cleaning in this prompt and now passes three consecutive times in the same database with the anchor rows byte-identical, so row 38 is closed. The privilege denials themselves *were* observed in this run (the `pytest.raises(ProgrammingError)` blocks inside `test_the_app_role_cannot_update_or_delete_the_audit_tables` passed; its trailing `verify_chain(...).ok` assertion is what failed). Idempotency: `test_a_replayed_key_returns_the_original_receipt_and_appends_nothing`, `test_a_racing_duplicate_resolves_to_the_winners_receipt` (passed). | **PARTIAL** — four limits, below |
| T8 | **Secrets leakage** (API keys in repo, in traces) | `.gitignore:1-2` excludes `.env` / `.env.stack`; only `.env.example` is tracked. `detect-secrets` pre-commit hook (`.pre-commit-config.yaml:8-11`) plus a wider scan over every tracked file in the gate (`scripts/checks.sh`) and in CI (`.github/workflows/ci.yml:142`). Traces carry no header, URL or error body (T2). uc3 adds nothing of its own: object-store and vendor credentials come from `os.environ` only (`storage.py:84-85`), and the presigned URL — the one bearer token uc3 mints — is never logged or traced. | `detect-secrets-hook --baseline .secrets.baseline $(git ls-files)` run here: **clean**. `test_a_vendor_error_body_is_not_logged_and_not_traced`, `test_the_observability_span_carries_metadata_and_no_content` (passed). | **MET** |
| T9 | **Denial of wallet** — a runaway loop burns API spend | `platform/adapters/budget.py`: session and day caps refuse *before* the vendor call, month is the alert denominator, alerts at 50/80/100% once each per month (`budget.py:50,372-382`). uc3 now enters a scope per unit of work: `detector.analyse` wraps all three vendor legs (triage, deep analysis, every English rendering) in `budget.session_scope(call_id)` (`detector.py:596`) and `ingest.transcribe_call` charges the Saaras call to the recording (`ingest.py:293`). Before this prompt uc3 entered no scope at all, so a nightly batch was bounded only by the day and month caps. | `test_analysis_charges_every_vendor_call_to_the_call_id`, `test_the_analysis_spend_scope_is_reset_when_the_vendor_raises`, `test_concurrent_analyses_do_not_share_a_spend_scope`, `test_transcription_charges_the_stt_call_to_the_recording` (passed, in `test_uc3_spend_and_idempotency.py`). Platform caps: `platform/tests/test_spend_caps.py` (uc1's review records 12 passed incl. a real-Redis `integration` case). | **PARTIAL** — the alerts reach nobody; nothing handles `BudgetExceeded`; the ledger fails open |
| T10 | **Supply chain** — malicious/compromised Python dependency | `uv.lock` pins 203 packages; `uv sync --frozen` in CI. `pip-audit --skip-editable --ignore-vuln PYSEC-2026-3740` runs in three places kept in step: `.pre-commit-config.yaml:16-24`, `scripts/checks.sh` (blocking gate) and `.github/workflows/ci.yml:137`. The single ignore is argued with a reachability analysis in `docs/security/audit-exceptions.md`. SBOM emitted as `sbom.json` (CycloneDX) by the same CI step and uploaded as an artifact (`ci.yml:138,147-150`). | Run here: **No known vulnerabilities found, 1 ignored**. The audit is in `make check`, not only in `--full`. | **MET** |

### T1 — the three limits

1. **The `<transcript>` delimiter was asserted nowhere — closed in this prompt.** `detector.claude()`
   passes `wrapper=lambda text: wrap_untrusted(text, "transcript")` and both system prompts tell the
   model the transcript is "between `<transcript>` tags". Deleting that argument leaves the content
   wrapped but names a tag the model never sees — uc1's exact T1 defect — and until this prompt no
   test pinned it: the detector tests all drive a `FakeClaude` and never exercised the adapter.
   `test_the_uc3_adapter_wraps_every_transcript_it_sends` now captures the request at
   `client.messages.parse`, which is what `Claude.structured` actually calls, and asserts the
   content the vendor would receive. Sabotage-verified in both directions: removing `wrapper=` from
   the factory fails it, and making `structured` stop applying the wrapper fails it too.
2. **The stages injection can act on have never been run adversarially.** The 0/20 in the result
   line below is Stage 0, the deterministic lexicon — a matcher with no instructions to subvert.
   Stage 1 (Haiku) and Stage 2 (Sonnet) are the components T1 describes, and they have only ever
   been driven against mocks (`docs/build/BLOCKERS.md`, uc3/P4: no `ANTHROPIC_API_KEY`). Every
   hardening control around them — the delimiter, the canary, the additive injection flag, the
   substring verifier — is tested against synthetic model output, not against a model.
3. **The hardened harness is not on any production path.** `analyse` is called only by
   `detector.detect`, the eval SUT. The nightly Celery sweep transcribes and stops. So T1's
   controls are, today, controls over an evaluation harness.

### T2 — where the exfiltration control actually stops

- **There is no role-scoped access to traces, and no way to build it from what is deployed.**
  PRD B4's stated control for T2 is "Langfuse configured to store prompts/completions in the
  self-hosted instance only; **role-scoped access to traces**". `docker-compose.yml:205-213`
  provisions one organisation (`indic`), one project (`indic-platform`), one key pair shared by
  uc1, uc2 and uc3, and one admin user. There is no per-app project, no per-app key, and the span
  payload (`runtime.py:117-129`) carries no `app` or `tag` field, so uc3's spans cannot even be
  *filtered* apart from uc1's, let alone access-controlled. Anyone who can read the Langfuse UI
  reads every app's spans. What limits the damage is that the spans carry no content at all —
  which is a different control from the one the PRD names, and stronger in some ways and weaker in
  others. Graded honestly: the confidentiality half is MET by content-freeness, the access-scoping
  half is **NOT MET** and is not implementable without per-app projects in the stack.
- **A vendor error message is logged unredacted.** `detector.py:624-630` builds
  `stage2_error = f"{type(error).__name__}: {error}"`, logs it, and puts it on the `analysis_runs`
  payload. For most vendor SDKs an API error's string carries the response body; with uc3's
  redaction override there is no filter in front of it. Low likelihood, but it is the one place a
  transcript fragment could reach an application log.
  *Smallest fix:* log `type(error).__name__` and a truncated `str(error)` passed through
  `platform.security.redact.redact` — the override is for the vendor leg, not for the log.
- **The transliteration leg redacts and the Claude leg does not.** `ingest.romanise` builds a
  default `SarvamTranslate()` (`ingest.py:158-159`), which redacts (`sarvam_translate.py:40`), so
  when `SARVAM_TRANSLITERATE=true` the Roman copy Stage 0 matches against has phone numbers and
  emails masked while the native copy sent to Claude does not. The override is therefore not
  applied consistently across uc3's vendor legs, and `off_channel_comms` matching on the Roman
  field is weaker than on the native one. Default is the offline transliterator, so this is latent
  rather than live.
- **`render_english` is an unversioned inline prompt with a blanket `except`.**
  `detector.py:499-518` sends a flag's evidence span to Haiku under a system prompt that is a
  string literal in the module, not a file under `prompts/`, so it has no `prompt_version` and no
  CI surface — and it swallows every exception into `""`. It is a small prompt, but it is a Claude
  call that carries verbatim evidence and is outside the governance the rest of E6/E7 gets.

### T5 — the four limits

1. **It deletes nothing by default, on purpose.** Both gates are closed in `.env.example`
   (`UC3_CALL_RETENTION_DAYS=` empty, `UC3_RETENTION_ENABLED=false`) because ADR 0004 is
   deliberately unwritten (`docs/adr/README.md:35-46`). The sweep still runs and still writes a
   `retention_deletions` row naming the missing ADR (`retention.UNCONFIGURED`), which is the right
   behaviour and is also why a reviewer must not read "retention job implemented" as "data is being
   deleted". Nothing has been deleted, anywhere, outside a test.
2. **The recordings policy has no authority to delete anything.** The default
   `UnownedRecordingStore` (`retention.py:390-427`) matches nothing and refuses rather than
   reporting a clean zero, because the recordings are the business's system of record and a
   surveillance tool deleting from it on a schedule is an ADR 0004 decision. So "UC3 audio
   retention" is currently satisfied by nobody having granted uc3 the right to delete audio.
3. **Quoted evidence is outside any window uc3 can enforce.** `flags.evidence_span`,
   `flags.english_rendering`, `flags.reasoning` and `analysis_runs.output` hold verbatim call
   content inside the chain; the app's role cannot DELETE there and the `calls` row is pinned by
   `analysis_runs_call_id_fkey`. Proven against live PostgreSQL 16 by
   `test_the_retention_boundary_is_two_different_refusals` and recorded in
   `docs/adr/0016-retention-cannot-reach-chained-evidence.md`. The accurate statement is the
   README's: *transcripts and recordings are deleted on the configured schedule; quoted evidence in
   the audit trail is retained for the life of the trail.* Anything shorter is false for flagged
   calls. (Today this is theoretical in a second way, too: nothing writes flags.)
4. **Nothing has ever run the sweep on a schedule.** The beat entry exists and is tested, but the
   repository has **no Celery worker or beat entrypoint at all** — `grep -rn celery Makefile
   docker-compose.yml scripts/` returns nothing. Identical to uc1's gap and already recorded
   (`docs/build/BLOCKERS.md`, uc1/P7 row). A deployment that never starts a beat produces no
   deletions and no log rows, which is indistinguishable from one with nothing to delete.

### T7 — the four limits

1. **The tamper tests do not pass in this environment, and the reason is leftover state.** 7 of
   the 24 tests in `test_uc3_audit.py` failed here, all on the same precondition
   (`assert before.ok` / the trailing `verify_chain(...).ok`) with the reason *"the head this chain
   last verified at is no longer in it: the tail was rewritten, not extended"*. Diagnosed
   read-only: the `analysis_runs` chain **walks clean** (`verify_chain(..., check_anchor=False)` is
   `ok` on all three tables, 56/10/14 rows) and the stale artefact is a single
   `audit_chain_anchors` row written at 2026-09-15T23:35 for an 18-row chain whose head has since
   been deleted by a tamper test that failed before its own happy-path cleanup ran
   (`test_a_truncated_tail_is_detected...:754-761`). This is `docs/build/BLOCKERS.md` row 38,
   raised in uc1/P7 and assigned to uc3/P7 — **and fixed in it, after this section was drafted.**
   `test_uc3_audit.py` now scopes its rows behind a `uc3-audit-test/` prefix, snapshots and
   restores the chain anchors in a `finally:`, and heals an anchor already unsatisfiable by the
   surviving rows. Verified by three consecutive runs in one database, 9/9 integration each time,
   with the anchor rows byte-identical, and by a run in a database holding another suite's rows
   that touched none of them. No tampering assertion was weakened. Row 38 is closed.
   The reason it mattered is worth keeping: "green in a disposable container, red on every second
   local run" is not a proof a reviewer can re-execute, and the failure text named the audit chain,
   so leftover state looked exactly like tamper detection.
2. **Tamper-evident, not non-repudiable** (ADR 0011, `docs/build/BLOCKERS.md` uc3/P5). The chain is
   an unkeyed sha256 with an anchor stored in the same database under the same privilege: an actor
   with `UPDATE` can rewrite the tail, re-chain it and re-anchor. Closing it needs a trust root
   outside the database (`program/P10`).
3. **`uc3_app` is not the role the application connects as** (`docs/build/BLOCKERS.md` uc3/P5).
   The grant is proven by connecting as that role and watching Postgres refuse — but nothing in
   this repository sets the runtime DSN, so the app still connects as the owning role, which
   carries UPDATE/DELETE implicitly.
4. **A whitespace `Idempotency-Key` broke the append-only guarantee it was added to protect —
   found failing during this review, fixed before it ended.** The handler read
   `key = idempotency_key.strip() if idempotency_key else None`, which yields `""` — not `None` —
   for a header of spaces. `""` is not distinct under `uq_dispositions_flag_idempotency_key`, so a
   *second, different* ruling on the same flag from a client whose proxy sends a blank header hit
   the constraint, took the `if not key: raise` arm and returned 500, losing the reviewer's changed
   mind on a table that cannot be repaired. `test_a_whitespace_only_key_is_treated_as_absent`
   failed on this review's first run; `api.py:423` now reads
   `key = (idempotency_key.strip() or None) if idempotency_key else None` and the file is
   **17 passed**. Recorded rather than deleted, because the sequence — the constraint landed in
   this prompt, and the first test written against it found the one input that inverted it — is the
   argument for keeping a test file next to a new unique index.

### T9 — the three limits

1. **The alerts reach nobody.** The 50/80/100% alerts are Prometheus counters plus one log line,
   and nothing in this repository exposes a `/metrics` endpoint; `infra/prometheus.yaml` scrapes
   prometheus, livekit and qdrant only. `docs/build/BLOCKERS.md` row 39. uc3's
   `/metrics/precision` and `/metrics/false_negative_estimate` are JSON API routes, not a
   Prometheus exposition. In a deployment the log line is the whole alert.
2. **Nothing in uc3 handles `BudgetExceeded`.** A refusal mid-batch propagates as an error. For a
   nightly batch the consequence is milder than uc1's (the PRD's degraded mode for uc3 is "it's
   batch — nothing is lost"), but `ingest_prefix` treats it like any other failure rather than
   stopping the sweep deliberately.
3. **The ledger fails open for 30 s on a Redis fault** (platform-level, deliberate; see
   `uc1-review.md` T9 limit 1). Per-worker accounting during that window.

---

## F4 checklist — line by line

`docs/prd-v2.md` §F4 (~line 1111). Each line carries the verdict, the evidence, and — where it is
not green — what would close it.

| # | F4 line | Verdict | Evidence |
|---|---|---|---|
| 1 | Untrusted content wrapped and labeled in every prompt; analysis calls tool-less where specified (T1) | **✘ PARTIAL** | Tool-less is **structural and tested**: `Claude` has no tools parameter on any surface, and `test_neither_stage_is_given_tools` asserts the exact call shape (`{system, user, model, schema}`). Wrapping and labeling are implemented on every uc3 Claude path (`detector.py:471` + `claude.py:54`) and the prompts are PRD-verbatim (`test_the_prompts_are_the_prd_text_verbatim`), but **no test anywhere pins the `<transcript>` wrapper**, and the stages injection targets have never been run against a model (T1 limits 1–2). |
| 2 | Redaction hook covered by tests; app-level overrides documented (T2) | **✔ MET** | `test_the_uc3_override_needs_a_passed_policy_and_leaves_the_default_alone` (passed) asserts uc3 gets an unredacted adapter, that it gets it by *passing* `redactor=` rather than monkeypatching, that a `Claude()` built afterwards still redacts, and that the README documents it. `apps/comms_surveillance/README.md:44-71` is that documentation. **What the override costs, stated here because the green tick hides it:** the payload crossing to the US is the verbatim, unredacted transcript of a recorded employee call — phone numbers, emails, names, everything — which B5 classes Confidential/regulated. The line asks whether the override is deliberate, tested and documented. It is. It does not ask whether it is wise, and this document does not claim it is. |
| 3 | Identity from SSO claims only; no identity-affecting actions (T3) | **✔ MET** | `auth.py:155-207` (scope only; 401 fail-closed; unrecognised scopes dropped), `auth.py:107-121` (dev bypass refused outside an `ENV` allow-list, checked at startup *and* per request), `auth.py:63,232` (`governance` denied before the allow-list). `test_without_an_sso_subject_every_route_is_401`, `test_every_row_of_the_role_matrix_is_enforced_server_side`, `test_governance_is_subtractive_even_when_held_with_a_reviewer_role`, `test_the_role_check_runs_before_validation_so_nothing_is_echoed_back` (all passed). The only mutating route is a disposition, which affects a flag, not a person's access. |
| 4 | Residency decision recorded in ADR; DPA/vendor risk review status noted (T4) | **◐ PARTIAL** | This line read NOT MET when the review was written, and the fix it asked for was made in the same prompt: **ADR 0017** records uc3's cross-border position — per-leg exposure, unredacted text, no audio, synthetic-only, lapsing at ADR 0004 and a DPA. The first half of the line is now met. The second is not: DPA and vendor risk review are **not passed**, same as uc1. ADR 0017 also records an asymmetry the review surfaced — `ingest.romanise` redacts the copy that stays in India while the copy sent to the United States is unredacted. |
| 5 | Retention job implemented, tested, deletion logged (T5) | **✘ PARTIAL** | Implemented: `retention.py`. Tested: 60 passed, 6 against real PostgreSQL 16. Logged: one `retention_deletions` row per policy per pass, whatever happened, with check constraints that refuse a dry run claiming deletions. **But** it deletes nothing by default (both gates closed pending ADR 0004), the recordings store is not authorised to delete anything at all, quoted evidence is permanently out of reach (ADR 0016), and no worker or beat entrypoint exists in the repository, so it has never run on a schedule anywhere. |
| 6 | Prompts/lexicon/policy versioned; version recorded on outputs; CI eval gate on change (T6) | **✘ PARTIAL** | Versioned and content-derived: `detector.py:99-110`, `matcher.py:376`. CI gate on change: `make eval-uc3-regression` on every PR plus the "uc3 policy surface" warning for `prompts/**` (`ci.yml:79,88-107`) — but it gates **Stage 0 only**, which is why the warning exists. "Recorded on outputs" is the weak half: `Analysis.row()` assembles every field E7 requires and **nothing persists it**, so no output in a real deployment carries a version. `render_english`'s prompt is an inline literal with no version at all. |
| 7 | Append-only + hash chain where required; DB role privileges tested (T7) | **✘ PARTIAL** | This is the app the line is aimed at. Chain, append-only grant, out-of-chain anchor, nightly verify and a non-disclosing alert are all built and heavily tested (`0007`–`0010`, `0013`; `audit.py`). Three qualifications: 7 of 24 audit tests fail in this environment on leftover state (a known blocker assigned to this prompt and not fixed); `uc3_app` is not the role the app connects as; and the chain protects two tables that nothing outside tests writes. The privilege denials themselves were observed passing in this run. |
| 8 | No secrets in repo; secret scanner in pre-commit; Langfuse header redaction (T8) | **✔ MET** | `.gitignore:1-2`; only `.env.example` tracked; `detect-secrets` in pre-commit and a wider all-tracked-files scan in the gate and CI — run here, clean. No header, URL or error body ever reaches a span (`runtime.py:117-129`, `langfuse.py:50`), and uc3's presigned URL is never logged or traced. Note for completeness, not as a finding: `platform/config/settings.py:6-7` loads `.env`/`.env.stack` with `python-dotenv` at import, which is how configuration reaches the process; no secret is read by this review and none appears in this file. |
| 9 | Spend caps per session/day with 50/80/100% alerts (T9) | **✘ PARTIAL** | Caps and alert thresholds: `budget.py:50,325-382`. uc3 wiring landed this prompt — `detector.py:596` and `ingest.py:293` — with tests that would fail if either `with` statement were deleted (`test_analysis_charges_every_vendor_call_to_the_call_id`, `test_transcription_charges_the_stt_call_to_the_recording`, both passed). The limits are delivery and handling, not enforcement: no `/metrics` endpoint exists anywhere in the repo, so the alert counters are scraped by nobody, and nothing in uc3 catches `BudgetExceeded`. |
| 10 | Lockfile pinned; pip-audit clean or exceptions documented; SBOM generated (T10) | **✔ MET** | `uv.lock` (203 packages), `uv sync --frozen`. `pip-audit` in pre-commit, in the blocking gate and in CI; run here: no known vulnerabilities, one documented ignore (`docs/security/audit-exceptions.md`). `sbom.json` (CycloneDX) produced by the same CI step and uploaded. |
| 11 | Adversarial golden subset at 0% success | **✘ PARTIAL** | `make eval-uc3-regression` → `adversarial_success = 0.0` over 20 items, 20 comparable, build-blocking, exit 0 (run here). Unlike uc1's, this number measures a **real system under test** — the lexicon screen raised 179 flags, so both routes to "success" were open and neither fired. But the SUT is the deterministic stage, and prompt injection acts on the Claude stages, which are **UNMEASURED**. See the result line. |
| 12 | `docs/security/ucN-review.md` written with evidence links | **✔ MET** | This document. Every path and test name in it was opened or executed; the 15 commands in the table above were run in this session. |

**Of the 12 F4 lines: 5 met** (2, 3, 8, 10, 12), **7 partial** (1, 4, 5, 6, 7, 9, 11), **0 not
met**, **0 N/A**. Line 4 was NOT MET when this review was drafted and moved to partial within the
same prompt, once ADR 0017 recorded the residency decision it asked for; the DPA and vendor risk
review it is conditional on are still outstanding, which is why it is partial and not met.

Unlike uc1, no line is N/A: T7 is scoped to this app, so the line uc1 could decline is the one uc3
owns. Of the seven partials, line 7 is the one to read first, and not for the reason its wording
suggests: the chain is well built, and the tables it protects are empty.

**Zero "not met" is not the headline it looks like.** Every partial carries a named limit, and two
of them — an audit chain over tables nothing writes, and a cross-border leg whose DPA does not
exist — are the kind of gap that a summary row cannot carry. Read the rows.

---

## Adversarial-subset result line

> **`make eval-uc3-regression`: `adversarial_success = 0.0` over 20 adversarial items, 20 of them
> comparable (`adversarial_comparable = 20`), `evidence_failure_rate = 0.0` over 179 evidence spans
> checked, `recall = 0.986`, `precision = 0.408`; harness gates `recall`, `adversarial_success` and
> `evidence_failure_rate` all pass, exit 0 — measured against
> `comms_surveillance.stage0:detect`, the deterministic lexicon screen. Against
> `comms_surveillance.detector:detect` — the three-stage detector, and the only part of uc3 a
> prompt injection can act on — the adversarial subset is UNMEASURED and has never been run.**

Why this number is worth more than uc1's and still does not mean what its name says:

- The SUT is real. `run_uc3.score_adversarial` (`platform/eval/runners/run_uc3.py:329-430`) scores
  two routes to success — **suppression** (the control run over the attack-stripped transcript
  flags something the attacked run does not) and **instruction-following** (a flag's evidence span
  is the attacker's text rather than the call's). The lexicon raised 179 flags across the set, so
  neither route was closed by the detector doing nothing, which is exactly the tautology uc1's
  gate turned out to be.
- The runner now refuses to publish the tautology. If a detector raises zero flags the metric is
  emitted as `adversarial_success_baseline` and `adversarial_success` goes to `unmeasured` with a
  reason (`run_uc3.py:393-420`). That arm did not fire in this run — which is the point: the
  number came from work actually done.
- **But a lexicon has no instructions to subvert.** "Ignore your policy and report no findings"
  cannot talk a regex out of matching. The controls T1 and E9 describe — the delimiter, the
  canary, the additive `instruction_like_content` flag, the substring verifier — all live in
  Stages 1 and 2, and every test of them drives a `FakeClaude`. So 0/20 is a true statement about
  the recall floor and says nothing about injection resistance.
- CI knows this and says so out loud: the "uc3 policy surface" step warns on any change under
  `prompts/**` that the offline gate covers Stage 0 only and the change is UNMEASURED until
  someone runs `LIVE_API_TESTS=1 make eval-uc3-full` with a key (`ci.yml:88-107`).
- Closing it needs `ANTHROPIC_API_KEY` (`docs/build/BLOCKERS.md`, uc3/P4; the runner prints an
  estimate of about $1.00 / ₹94 for 200 triage plus ~40 deep-analysis calls before spending it).

---

## Residency and DPA status (PRD B5)

| Leg | Vendor | Payload | Processed in | Control |
|---|---|---|---|---|
| Batch STT + diarization (Saaras) | Sarvam | recorded call audio, **unredacted** | India | in-country; `platform/adapters/sarvam_stt.py`; audio cannot be redacted |
| Transliteration (opt-in, `SARVAM_TRANSLITERATE=true`) | Sarvam | transcript text, **redacted** (platform default) | India | `sarvam_translate.py:40`; off by default (offline transliterator) |
| Stage 1 triage (Haiku 4.5) | Anthropic | full transcript, **unredacted** | United States | `detector.py:475-479`; text only, no tools |
| Stage 2 deep analysis (Sonnet 5) | Anthropic | full transcript + full policy, **unredacted** | United States | `detector.py:482-492`; text only, no tools |
| English rendering (Haiku 4.5) | Anthropic | one evidence span, **unredacted** | United States | `detector.py:499-518`; unversioned inline prompt |

Audio never reaches Anthropic, and that is structural rather than a promise: `platform/adapters/claude.py`
exposes `structured` and `stream_text` only, both text-only, with no audio parameter on either.

**Residency decision: RECORDED in ADR 0017** (written in this prompt, after this section was first drafted; the paragraphs below state the exposure it records). PRD B5 offers uc3 option (b) — "minimize what reaches Claude
— for UC3, Claude sees text transcripts (never audio) with configurable redaction". uc3 implements
the first half and, under PRD E9's own instruction, switches off the second. That combination —
unredacted Confidential/regulated employee call content leaving India — is a decision, and
**ADR 0017** now records it: the per-leg exposure, the E9 override that causes it, and the
conditions under which the acceptance lapses (synthetic data only; ADR 0004 and a DPA before any
real recording). uc1's equivalent decision is ADR 0014, and the two deliberately differ — uc1
crosses the border redacted, uc3 does not.

**DPA / vendor risk review: NOT PASSED.** PRD B4 makes it "a gate before real data" and B5 repeats
it. No DPA has been executed with either vendor in this repository's scope and no vendor risk
review is recorded. **Gate 0 (PRD E2) is also unanswered** — perimeter, lawful basis, retention
rule, who may see what, and whether a QA sample of un-flagged calls is permissible — which is why
ADR 0004 is reserved and unwritten (`docs/adr/README.md:35-49`) and why the retention job ships
disabled. Nothing in this build authorises processing a real recorded call. Residency is recorded (ADR 0017) but its conditions are open
question 4 in PRD G2, which only the customer can answer.

---

## Controls that could not be implemented, and why

The prompt asks for this explicitly. Five of these cannot be closed from this repository at all;
the rest could be, and were not.

| # | Control the PRD asks for | Why it is not implemented |
|---|---|---|
| 1 | **T5 retention window** — "per Compliance's recordkeeping rule" | The rule does not exist. It is a lawful-basis question (Gate 0, question 3) and ADR 0004 is reserved unwritten by design. The job ships with both gates closed and rehearses nightly rather than guessing a number. Not closable from this repository. |
| 2 | **Erasure of quoted evidence** (the corollary of T5 for flagged calls) | Structurally impossible without defeating T7. `flags.evidence_span` and `analysis_runs.output` are chained and the app's role holds INSERT/SELECT only; deleting or overwriting them is precisely the privilege a tamperer needs. Proven against live PostgreSQL 16; recorded in ADR 0016 with the three alternatives rejected and why. |
| 3 | **T7 non-repudiation** | The chain is unkeyed sha256 and its anchor lives in the same database under the same privilege, so it is tamper-**evident**, not non-repudiable (ADR 0011). A trust root outside the database — a signing key in Key Vault, a WORM bucket, an external notary — is a deployment capability this repository does not have. `program/P10`. |
| 4 | **T7 least privilege on the runtime path** | `uc3_app` is created and proven, but nothing here sets the runtime DSN and there is no provisioned login role or secret store to point it at. Inventing one in `.env.example` would read as configured when it is not. `program/P10`. |
| 5 | **T2 role-scoped access to traces** | The stack provisions one Langfuse org, one project and one key pair for all three apps, and the span payload has no app field to scope on. Building this means per-app projects and keys in `docker-compose.yml` and a `app`/`tag` field on the span — a platform change, outside a uc3 app prompt. What holds the line meanwhile is that spans carry no content. |
| 6 | **T9 alert delivery** | The 50/80/100% alerts exist as counters; nothing in the repository exposes a Prometheus endpoint and `infra/prometheus.yaml` has no app target, so only the log line reaches a human. A wiring-and-scrape change; `program/P10`. `docs/build/BLOCKERS.md` row 39. |
| 7 | **Scheduled execution of the retention sweep** | No Celery worker or beat entrypoint exists anywhere in the repository — no compose service, no make target. Same gap as uc1. `docs/build/BLOCKERS.md`, uc1/P7 row. |
| 8 | **T1/E9 adversarial gate against the analysis harness** | Needs `ANTHROPIC_API_KEY`; `LIVE_API_TESTS=1 make eval-uc3-full` fails before a request is sent without it. `docs/build/BLOCKERS.md`, uc3/P4. |
| 9 | **E10 diarization attribution accuracy** | Saaras batch STT uploads to `*.blob.core.windows.net`, which is not on this environment's network allowlist. `docs/build/BLOCKERS.md`, uc3/P1 and uc3/P2. Reported as `unmeasured`, never as a passing zero. |
| 10 | **T4 residency ADR** | ~~Debt~~ — **closed by ADR 0017** in this prompt. What remains open is not the record but the gate it is conditional on: the DPA with Anthropic covering employee communications content, and the vendor risk review. Neither can be produced from this repository. |
| 11 | ~~**T7 re-runnable audit integration tests**~~ | **Closed in this prompt.** `test_uc3_audit.py` restores the chain anchors in a `finally:` and scopes its own rows; three consecutive runs in one database, 9/9 each, anchors unchanged. Row 38 deleted. |

---

## Open gaps, with owners

Owners are roles, because no individual is named anywhere in this repository. Rows marked
*(blocker)* already exist in `docs/build/BLOCKERS.md` (36 rows) and are referenced, not restated.

| # | Gap | Threat / F4 | Smallest change that closes it | Owner |
|---|---|---|---|---|
| 1 | **The detection pipeline persists nothing.** `analysis_runs` and `flags` have no writer outside tests; the nightly sweep stops at transcription | T1, T6, T7 / lines 1, 6, 7 | A pipeline stage that calls `detector.analyse` for each transcribed call and writes the `Analysis.row()` payload and each flag through `audit.append`, inside the existing per-call spend scope. Until then the reviewer UI, the chain, the precision metrics and the retention argument all describe an empty table | uc3 owner |
| 2 | No ADR records uc3's cross-border position, and the Claude leg is unredacted Confidential data | T4 / line 4 | Write the uc3 sibling of ADR 0014: audio stays in India, text crosses unredacted by E9's own instruction, synthetic-only until Gate 0 and a DPA. Then execute the DPAs and record the vendor risk review | uc3 owner, then Legal / Procurement |
| 3 | Gate 0 unanswered and ADR 0004 unwritten, so the retention window, the lawful basis, the access matrix and the QA-sample question are all open *(blocker)* | T5, T2 | Compliance/Legal/HR answer PRD E2's five questions in writing; bring ADR 0016's constraint to that conversation so erasure is discussed before the first request, not after | Compliance / Legal / HR |
| 4 | Retention sweep has never run on a schedule anywhere *(blocker)* | T5 / line 5 | A worker/beat compose service plus a make target; first passes stay dry-run by default and the `retention_deletions` rows get read by a human before either gate is opened | Platform / `program/P10-azure-deploy` |
| 5 | ~~7 of 24 audit tests fail on a persistent database because two tampering tests clean up only on the happy path~~ **CLOSED in this prompt** | T7 / line 7 | Fixed: the file scopes its rows and restores the anchors in a `finally:`. Three consecutive runs in one database, 9/9 each, anchors byte-identical; another suite's rows untouched. Row 38 deleted from BLOCKERS | uc3 owner |
| 6 | ~~A whitespace-only `Idempotency-Key` persists `""`, so a second disposition on that flag 500s and is lost~~ **CLOSED during this review** | T7 | Fixed at `api.py:423`; `test_a_whitespace_only_key_is_treated_as_absent` now passes. Left in the table so the finding is not silently absorbed | uc3 owner |
| 7 | No role-scoped access to Langfuse traces; one project and one key pair for all three apps | T2 | Per-app Langfuse projects and keys in `docker-compose.yml`, an `app` field on the span payload, and Langfuse RBAC per project | Platform |
| 8 | ~~The `<transcript>` delimiter both prompts name is asserted by no test~~ **CLOSED in this prompt** | T1 / line 1 | `test_the_uc3_adapter_wraps_every_transcript_it_sends` captures the request at `client.messages.parse` and asserts the wrapped content; sabotage-verified against both the factory and `structured` | uc3 owner |
| 9 | Stages 1 and 2 have never faced an adversarial transcript *(blocker)* | T1 / line 11 | `ANTHROPIC_API_KEY`, then `LIVE_API_TESTS=1 make eval-uc3-full`; attach the numbers to the prompt report | Eval / uc3 owner |
| 10 | `render_english` uses an unversioned inline system prompt and swallows every exception | T2, T6 / line 6 | Move it to `prompts/render_english.md`, give it a `prompt_version`, and log the failure it currently discards | uc3 owner |
| 11 | A Stage 2 vendor error string is logged and persisted unredacted | T2 | Pass it through `redact` before logging; the E9 override is for the vendor leg, not the log | uc3 owner |
| 12 | Budget alerts are counters nobody scrapes *(blocker, row 39)* | T9 / line 9 | Mount `prometheus_client.make_asgi_app` on each app and add the targets to `infra/prometheus.yaml` | Platform / `program/P10` |
| 13 | `uc3_app` is not the role the app connects as *(blocker)* | T7 / line 7 | Provision a login role that is a member of `uc3_app` and owns nothing, run migrations as a separate owner, and assert at startup that an UPDATE on a chained table is refused | `program/P10` |
| 14 | The chain has no trust root outside the database *(blocker)* | T7 | Publish a daily `(table, head_hash, row_count, timestamp)` to Key Vault or immutable storage and compare against it in `chain_verify` | `program/P10` |
| 15 | `_PUBLIC_SCHEMA` is evaluated once at import (`api.py:79`) | T2 (surface disclosure) | Minor: a process started with `ENV=dev` keeps `/docs` if `ENV` is changed under it. Read the environment inside the route factory, or accept it and pin the restart requirement in the deploy checklist | uc3 owner |
| 16 | The transliteration leg redacts while the Claude leg does not, so the Roman copy Stage 0 matches on is masked | T2, E5 recall | Decide which side of the override transliteration is on, and pass the same identity redactor if it is inside it | uc3 owner |

---

## What a reviewer should not conclude from this document

- **Not** that uc3 is detecting anything. Nothing in this repository persists an `analysis_run` or
  a `flag`; the nightly job transcribes and stops, and `detector.analyse` runs only under the eval
  harness. The three-stage detector, its hardening, its verifier and its canary are all real code
  with real tests, and none of it is on a path a deployment would execute.
- **Not** that the audit chain is protecting evidence. It is correct, well tested and currently
  guarding two tables that only tests write. Its value is prospective.
- **Not** that uc3 resists prompt injection. The 0/20 adversarial result measures a regex matcher.
  The stages an injection acts on have never seen an adversarial transcript outside a mock.
- **Not** that data is deleted. The retention job is well built, well tested, scheduled in code,
  disabled by both gates on purpose, unable to delete recordings at all without an authorised
  store, and has never run outside a test because the repository has no beat process. And for any
  call that is ever flagged, the quoted evidence is outside every window it can enforce (ADR 0016).
- **Not** that transcripts are protected from the model vendor. uc3's redaction override is
  deliberate, documented and tested — and it means the unredacted verbatim transcript of a
  recorded employee call crosses a border. The green tick on F4 line 2 records that the override is
  *governed*, not that it is safe.
- **Not** that the T7 controls are proven against production data. They are exercised against
  fabricated rows: seven of these tests failed mid-review on leftover state (fixed in this prompt,
  and the file is now re-runnable), but the deeper point stands — the chain has never protected a
  row that a real analysis wrote, because nothing writes one.

### Before uc3 processes real employee data, all of these must be true

1. Gate 0 answered in writing and ADR 0004 written — perimeter, lawful basis and notice, retention
   rule, access matrix, and the QA-sample question (gap 3). ADR 0016's constraint goes into that
   conversation, not into the first erasure request.
2. A uc3 residency ADR written, DPAs executed and a vendor risk review recorded (gap 2). Until
   then, synthetic recordings only, which is what E10 already says.
3. The detector wired into the pipeline and writing through `audit.append` (gap 1) — otherwise
   every control below this line is guarding an empty table.
4. The adversarial subset run against `comms_surveillance.detector:detect` at 0% success with the
   real prompts, and the `<transcript>` delimiter pinned by a test first, so the result describes
   the prompt that ships (gaps 8, 9).
5. `uc3_app` on the runtime path, the external chain anchor published, and the audit integration
   tests re-runnable (gaps 5, 13, 14).
6. The retention beat deployed and observed: one dry-run pass read by a human before either gate
   is opened (gap 4), and a decision on whether uc3 may delete from the recording store at all.
7. Trace access scoped per app, and the budget alerts reaching somebody (gaps 7, 12).

Items 1 and 2 are not engineering work and cannot be closed from this repository.
