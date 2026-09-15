# Session kickoffs — paste one per cloud session

Environment `indicai`, repository `adommeti/IndicAI`, branch `main`, permission mode **Auto**.
`/run-prompt` only exists once this harness is on `main`; before the first session confirm with
`git ls-tree --name-only origin/main .claude`. Typing `/run-prompt` with no argument always selects
the correct next row from the table below, so it is the safest kickoff.
Each session runs one prompt to a merged PR (see `docs/build/RUNBOOK.md`). Rows are in dependency order;
tracks marked with the same letter can run in parallel sessions once their first row is merged.

Standard tail for every kickoff (already implied by the `run-prompt` skill; include it anyway):

```
Work autonomously to completion. Do not ask me questions; decide, record decisions in the report,
ship with scripts/ship.sh, and end only when the PR is merged or a hard blocker is recorded in
docs/build/BLOCKERS.md.
```

| # | track | kickoff message | needs merged | verify |
|---|---|---|---|---|
| 1 | — | `/run-prompt program P1-golden-audio` (or just `/run-prompt`) — Restore + commit UC1 golden WAVs; eval runs in a fresh clone | uc1/P2 | S,K,L |
| 2 | A | `/run-prompt uc1 P3` — LangGraph agent (chat-only) with deterministic guard | program/P1-golden-audio | S,C,K,L |
| 3 | A | `/run-prompt uc1 P4` — Zammad ticketing, idempotent, Celery fallback | uc1/P3 | S,C,K |
| 4 | A | `/run-prompt uc1 P5` — Voice pipeline: LiveKit + Pipecat + Saaras + Bulbul | uc1/P4 | S,K,L |
| 5 | A | `/run-prompt uc1 P6` — React widget, replay endpoint, Grafana dashboard | uc1/P5 | S,C,K |
| 6 | A | `/run-prompt uc1 P7` — Security + retention + spend caps; docs/security/uc1-review.md | uc1/P6 | S,C |
| 7 | B | `/run-prompt uc2 P0-spike` — Vendor contract spike → docs/adr/0003 | program/P0 | L |
| 8 | B | `/run-prompt uc2 P1` — Terminology files, golden set, eval runner | uc2/P0-spike | S,L |
| 9 | B | `/run-prompt uc2 P2` — Celery pipeline: adapt → translate → post_edit → QA → quiz | uc2/P1 | S,C,K,L |
| 10 | B | `/run-prompt uc2 P3` — Reviewer UI with LOCKED enforcement | uc2/P2 | S,C |
| 11 | B | `/run-prompt uc2 P4` — Production: dubbing, TTS, VTT, packaging | uc2/P3 | S,K,L |
| 12 | B | `/run-prompt uc2 P5` — Pilot delivery page, quiz, comprehension report | uc2/P4 | S,C,K |
| 13 | C | `/run-prompt uc3 P1` — Synthetic golden set (200), audio subset, eval runner | program/P0 | S,L |
| 14 | C | `/run-prompt uc3 P2` — Ingestion + batch STT/diarization + transliteration | uc3/P1 | S,C,K,L |
| 15 | C | `/run-prompt uc3 P3` — Lexicon matcher (Stage 0) | uc3/P2 | S |
| 16 | C | `/run-prompt uc3 P4` — Haiku triage, Sonnet deep analysis, verifier, canary | uc3/P3 | S,L |
| 17 | C | `/run-prompt uc3 P5` — Append-only audit schema with hash chain, DB roles | uc3/P4 | S,C,K |
| 18 | C | `/run-prompt uc3 P6` — Reviewer UI, roles, metrics | uc3/P5 | S,C |
| 19 | C | `/run-prompt uc3 P7` — Security review, retention, spend caps; CI policy gate | uc3/P6 | S,C |
| 20 | — | `/run-prompt program P9-adrs` — ADRs 0001/0002/0005–0008 + index | program/P0 | S |
| 21 | — | `/run-prompt program P8-eval-refactor` — Shared eval harness: common schema, judge, `make eval` | uc1/P3,uc2/P2,uc3/P4 | S,K,L |
| 22 | — | `/run-prompt program P10-azure-deploy` — Bicep single-VM POC, Key Vault, OIDC deploy workflow | uc1/P7,uc2/P5,uc3/P7 | S,C |
| 23 | — | `/run-prompt program P11-release-readiness` — README, demo, pilot checklist, release eval, tag | program/P8-eval-refactor,program/P10-azure-deploy,program/P9-adrs | S,C,K,L |

## Range sessions (fewer sessions, same guarantees)

```
/run-prompt uc1 P3..P7
/run-prompt uc2 P0-spike..P5
/run-prompt uc3 P1..P7
/run-prompt program P9-adrs..P11-release-readiness
```

Each prompt in a range is merged before the next starts; a red gate or a blocker stops the range at that prompt
and the final message says where to resume (`/run-prompt` with no arguments resumes at the next pending prompt).

## Recovery messages

- Gate kept failing / session ended unshipped: `Reopen the branch build/<uc>-<pn>, read .claude/run/gate.log, fix, and ship.`
- CI red after merge attempt: `Read gh pr checks on the open PR for build/<uc>-<pn>, fix the failure, and re-run scripts/ship.sh.`
- Two alembic heads after parallel tracks: `Create an alembic merge revision for the current heads and ship.`
