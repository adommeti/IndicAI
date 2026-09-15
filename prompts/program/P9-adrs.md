# P9-adrs — Architecture decision records

<!-- Run: bash scripts/run-prompt.sh program P9-adrs -->
Read CLAUDE.md and docs/prd-v2.md Part G1. Create `docs/adr/` with the MADR-style template `docs/adr/0000-template.md` (status, context, decision, consequences, evidence) and write:

- `0001-reason-in-users-language.md` — Claude reasons in the user's language; translation only for English artifacts and as an optional retrieval booster (cite the UC1 P2 hit@3 with/without parallel translate from `docs/eval/uc1.json` if present).
- `0002-one-platform-three-apps.md` — shared platform package + one Compose stack; import name `indic_platform`; adapters as the only vendor boundary.
- `0005-hybrid-detection-hardened-analysis.md` — lexicon → Haiku → Sonnet with tool-less, verified, canaried analysis.
- `0006-meeting-minutes-separate-module.md` — separate consent/data path.
- `0007-build-workflow.md` — one PR per build prompt, squash merges, CI gates (attribution, checks, integration), how prompts are sequenced (`docs/build/PROMPT-PLAN.md`), and the three verification tiers (in-session, CI service containers, local full stack/live).
- `0008-cloud-build-environment.md` — the cloud environment contract: setup script, network allowlist (Sarvam API, Hugging Face for TEI weights, package registries), env variables, 16 GB memory budget and the stack profiles.

Leave 0003 (dubbing contract) and 0004 (surveillance scope) to their owning prompts; if they already exist, link them from a `docs/adr/README.md` index. Update `README.md` to point to the ADR index.
Acceptance: six ADRs + template + index exist, each with a "Consequences" section and links to code/tests where a decision is already implemented; `make check` green.

## Execution notes
- No stack or vendor calls needed. Keep each ADR under 80 lines; decisions, not essays.
