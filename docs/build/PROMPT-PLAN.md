# Build plan — ordered prompt sequence

Source of truth for sequencing and status. `scripts/run-prompt.sh` reads the table below:
`status` ∈ `done | pending | active | partial | blocked`; `needs` lists prerequisite prompts
(`group/prompt`, comma-separated, `—` for none). Only the row being shipped changes in a PR.

Verification tiers: **S** = in-session (`make check`: lint, typecheck, unit, offline evals);
**C** = CI service containers (`make test-integration`, migrations round-trip);
**K** = local Docker stack in the session (`make stack-*`, `make eval-ucN`, `make voice-test`);
**L** = live vendor calls (`LIVE_API_TESTS=1`, `sarvam` MCP, dubbing) — cost noted in each prompt.

| group | prompt | status | needs | title | verify | merged |
|---|---|---|---|---|---|---|
| program | P0 | done | — | Scaffold: uv workspace, adapters, compose, CI | S,K,L | 027f125…0bf1475 |
| uc1 | P1 | done | program/P0 | Golden set (150 items) + eval runner | S,L | 0bf1475 |
| uc1 | P2 | done | uc1/P1 | KB ingestion + cross-lingual hybrid retrieval | S,K,L | 0bf1475 |
| program | P1-golden-audio | partial | uc1/P2 | Golden WAVs committed and hash-verified; regeneration tool still to build | S,K,L | pending PR |
| uc1 | P3-eval | blocked | uc1/P3 | Measure the UC1 P3 B6 gates (action accuracy, hit@3, adversarial, language match) | K,L | see docs/build/BLOCKERS.md |
| uc1 | P3 | done | uc1/P2 | LangGraph agent (chat-only) with deterministic guard | S,C,K,L | pending PR |
| uc1 | P4 | partial | uc1/P3 | Zammad ticketing, idempotent, Celery fallback | S,C,K | idempotency proven on Postgres; no live Zammad (no Docker), see BLOCKERS |
| uc1 | P5 | partial | uc1/P4 | Voice pipeline: LiveKit + Pipecat + Saaras + Bulbul | S,K,L | pipeline, consent gate and latency write path built and tested; the p50/p95 gate, action accuracy and the browser demo are UNMEASURED (no Docker, so no LiveKit) and no consent-notice asset exists, see BLOCKERS |
| uc1 | P6 | partial | uc1/P5 | React widget, replay endpoint, Grafana dashboard | S,C,K | widget, governance-gated replay and the dashboard built; Grafana provisioning wired for the first time (no dashboard here had ever been loaded). Chrome end-to-end and "dashboard renders" are UNMEASURED (no browser, no Docker); no LiveKit token endpoint exists. Run after uc1/P7 at the user's request. See BLOCKERS |
| uc1 | P7 | partial | uc1/P6 | Security + retention + spend caps; docs/security/uc1-review.md | S,C | redaction proven on the wire and a 4-3-3 mobile leak fixed; spend caps, retention sweep and the CI policy gate in place. F4 is not all green: the DPA gate is unpassed, retention has never been scheduled, the audio policy matches nothing, and CI gates the adversarial threshold but not agent quality. Run before uc1/P6 at the user's request. See BLOCKERS |
| uc2 | P0-spike | partial | program/P0 | Vendor contract spike → docs/adr/0003 | L | see docs/build/BLOCKERS.md |
| uc2 | P1 | done | uc2/P0-spike | Terminology files, golden set, eval runner | S,L | judge measured live 2026-09-17: fidelity 4.56 over 90 draft references (8 below 4, listed in the golden README). This is a draft-reference number, not the B6 product gate |
| uc2 | P2 | partial | uc2/P1 | Celery pipeline: adapt → translate → post_edit → QA → quiz | S,C,K,L | see docs/build/BLOCKERS.md |
| uc2 | P3 | partial | uc2/P2 | Reviewer UI with LOCKED enforcement | S,C | see docs/build/BLOCKERS.md |
| uc2 | P4 | partial | uc2/P3 | Production: dubbing, TTS, VTT, packaging | S,K,L | see docs/build/BLOCKERS.md |
| uc2 | P5 | partial | uc2/P4 | Pilot delivery page, quiz, comprehension report | S,C,K | see docs/build/BLOCKERS.md |
| uc3 | P1 | partial | program/P0 | Synthetic golden set (200), audio subset, eval runner | S,L | diarization accuracy unmeasured, see BLOCKERS |
| uc3 | P2 | partial | uc3/P1 | Ingestion + batch STT/diarization + transliteration | S,C,K,L | golden ingest and diarization accuracy blocked, see BLOCKERS |
| uc3 | P3 | done | uc3/P2 | Lexicon matcher (Stage 0) | S | |
| uc3 | P4 | partial | uc3/P3 | Haiku triage, Sonnet deep analysis, verifier, canary | S,L | B6 gates unmeasured, no ANTHROPIC_API_KEY, see BLOCKERS |
| uc3 | P5 | done | uc3/P4 | Append-only audit schema with hash chain, DB roles | S,C,K | chain verify 10003 rows in 0.46s; app role denied UPDATE/DELETE |
| uc3 | P6 | partial | uc3/P5 | Reviewer UI, roles, metrics | S,C | role matrix enforced and mutation-checked; live end-to-end demo and MSAL blocked, see BLOCKERS |
| uc3 | P7 | partial | uc3/P6 | Security review, retention, spend caps; CI policy gate | S,C | retention sweep (disabled until ADR 0004, and it cannot reach chained evidence -- ADR 0016), per-call spend scope, TLS-by-default recording grants, disposition idempotency, and a Stage 0 regression gate in CI. Adversarial success on the real three-stage detector is UNMEASURED (no ANTHROPIC_API_KEY); nothing persists analysis_runs/flags outside tests, so the chain guards two empty tables; Langfuse has no per-app scoping. See BLOCKERS |
| program | P9-adrs | done | program/P0 | ADRs 0001/0002/0005–0008 + index | S | pending PR |
| program | P8-eval-refactor | pending | uc1/P3,uc2/P2,uc3/P4 | Shared eval harness: common schema, judge, `make eval` | S,K,L | |
| program | P10-azure-deploy | pending | uc1/P7,uc2/P5,uc3/P7 | Bicep single-VM POC, Key Vault, OIDC deploy workflow | S,C | |
| program | P11-release-readiness | pending | program/P8-eval-refactor,program/P10-azure-deploy,program/P9-adrs | README, demo, pilot checklist, release eval, tag | S,C,K,L | |

## Recommended execution order and parallelism

Sequential by default. Each prompt is one cloud session (one branch, one PR, merged before the
next starts). The tracks below are independent after their first row and can run as parallel
sessions; the shared files they touch (`pyproject.toml` testpaths, `Makefile`, alembic revisions)
are reconciled by `scripts/ship.sh`'s rebase — if two revisions create two alembic heads, the
next ship adds a merge revision (see `.claude/rules/migrations.md`).

0. `uc1/P3` is already built and merged; its B6 numbers are still unmeasured (no golden audio, no live run yet)
1. `program/P1-golden-audio` → `uc1/P3-eval` → `uc1/P4` → `uc1/P5` → `uc1/P6` → `uc1/P7`
2. `uc2/P0-spike` → `uc2/P1` → `uc2/P2` → `uc2/P3` → `uc2/P4` → `uc2/P5`   (parallel to 1 after P0-spike)
3. `uc3/P1` → `uc3/P2` → `uc3/P3` → `uc3/P4` → `uc3/P5` → `uc3/P6` → `uc3/P7`   (parallel to 1 and 2)
4. `program/P9-adrs` (any time), then `program/P8-eval-refactor`, `program/P10-azure-deploy`, `program/P11-release-readiness`

Estimated sessions: 26. Estimated vendor spend across the whole build at the notes' figures:
≈ ₹1,500 (Sarvam: STT for evals, TTS for golden audio, one dubbing spike + one module dub) and
≈ $20–30 (Anthropic: decisions, judges, triage/deep analysis), excluding Claude Code usage itself.
