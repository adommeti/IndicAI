# P11-release-readiness — Final integration, documentation and pilot handoff

<!-- Run: bash scripts/run-prompt.sh program P11-release-readiness -->
Read CLAUDE.md and docs/build/PROMPT-PLAN.md. Every UC prompt and the program prompts P8–P10 are merged. Make the repository releasable as a POC.

1. Rewrite `README.md` from "P0 scaffold" to the platform: purpose, the three apps with one-paragraph descriptions and screenshots (`docs/build/screenshots/`), architecture diagram (Mermaid, from PRD B1 updated to what was built), quick start (local and Azure), stack profiles, eval commands and the latest measured numbers table (from `make eval`), security posture summary linking the three review documents, and the ADR index.
2. `docs/build/DEMO.md`: a 20-minute end-to-end demo script across the three apps with exact commands, expected outputs and fallbacks if a vendor is down.
3. `docs/build/PILOT-CHECKLIST.md`: per UC, the Gate 0 / consent / DPA / residency items from PRD B5, E2, G2 that must be answered before real data, with owner columns left blank.
4. Run `make check-full` and `make eval` on the full stack; fix anything red; record the final numbers in `docs/eval/RELEASE-<date>.md` (this one file is committed) with the B6 gate table showing PASS/FAIL/UNMEASURED per metric.
5. Dependency hygiene: `uv lock --upgrade` only for patch/minor updates that keep `make check-full` green; `pip-audit` clean or exceptions documented in `docs/security/dependency-exceptions.md`; SBOM committed as `docs/security/sbom.json`.
6. Tag the release `v0.1.0-poc` after merge (annotated tag, message "POC release"): add a `make tag-release VERSION=` target and document it; the tag itself is created by the owner.
Acceptance: README/DEMO/PILOT docs complete; `make check-full` green; release eval file committed with every B6 metric measured or explicitly unmeasured with the reason; `docs/build/PROMPT-PLAN.md` shows every prompt `done` or `partial` with a blocker link.

## Execution notes
- This is the only prompt allowed to touch every app. Keep changes to docs, dependency bumps, and small fixes needed to make `make check-full` green.
- Live evals here are the expensive ones (UC1 STT, UC2 judge, UC3 triage/deep): ≈ ₹300 + $5 total; print estimates and run each once.
