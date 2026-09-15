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
| uc1 | P4 | pending | uc1/P3 | Zammad ticketing, idempotent, Celery fallback | S,C,K | |
| uc1 | P5 | pending | uc1/P4 | Voice pipeline: LiveKit + Pipecat + Saaras + Bulbul | S,K,L | |
| uc1 | P6 | pending | uc1/P5 | React widget, replay endpoint, Grafana dashboard | S,C,K | |
| uc1 | P7 | pending | uc1/P6 | Security + retention + spend caps; docs/security/uc1-review.md | S,C | |
| uc2 | P0-spike | pending | program/P0 | Vendor contract spike → docs/adr/0003 | L | |
| uc2 | P1 | pending | uc2/P0-spike | Terminology files, golden set, eval runner | S,L | |
| uc2 | P2 | pending | uc2/P1 | Celery pipeline: adapt → translate → post_edit → QA → quiz | S,C,K,L | |
| uc2 | P3 | pending | uc2/P2 | Reviewer UI with LOCKED enforcement | S,C | |
| uc2 | P4 | pending | uc2/P3 | Production: dubbing, TTS, VTT, packaging | S,K,L | |
| uc2 | P5 | pending | uc2/P4 | Pilot delivery page, quiz, comprehension report | S,C,K | |
| uc3 | P1 | pending | program/P0 | Synthetic golden set (200), audio subset, eval runner | S,L | |
| uc3 | P2 | pending | uc3/P1 | Ingestion + batch STT/diarization + transliteration | S,C,K,L | |
| uc3 | P3 | pending | uc3/P2 | Lexicon matcher (Stage 0) | S | |
| uc3 | P4 | pending | uc3/P3 | Haiku triage, Sonnet deep analysis, verifier, canary | S,L | |
| uc3 | P5 | pending | uc3/P4 | Append-only audit schema with hash chain, DB roles | S,C,K | |
| uc3 | P6 | pending | uc3/P5 | Reviewer UI, roles, metrics | S,C | |
| uc3 | P7 | pending | uc3/P6 | Security review, retention, spend caps; CI policy gate | S,C | |
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
