# P6-security — Security, roles and spend caps for UC2; docs/security/uc2-review.md

<!-- Run: bash scripts/run-prompt.sh uc2 P6-security -->
Read CLAUDE.md, `docs/prd-v2.md` Parts B4 (T1-T10), B5, D3 and F4, and the two existing reviews
(`docs/security/uc1-review.md`, `uc3-review.md`). **UC2 has never had a security pass, and the PRD
is why**: Part D13's uc2 sequence runs P0-spike->P5 with no security prompt, while UC1 and UC3 each
have one. F4 says the checklist is "used by every app's P7", so uc2 fell through a hole in the
specification. Record that omission as an ADR, then close the gaps.

1. **Roles (T3, PRD D3).** `api.py` uses a single `authenticated_owner` dependency for module
   upload, review, approve, quiz attempt and the governance report, so a module owner can approve
   their own LOCKED compliance segments. Split it into the four D3 personas, enforced server-side,
   with a role-matrix test of the uc3 kind (`apps/comms_surveillance/auth.py` is the working model).
   UI hiding is not access control.
2. **Spend caps (T9, B8).** `grep session_scope apps/training_localizer` returns nothing: UC2 is the
   most expensive pipeline in the programme (dubbing Rs 40/min, five Claude stages, Bulbul TTS) and
   is bounded only by day and month caps. Wrap each module-language unit of work in
   `budget.session_scope(...)` and add a test that fails if the `with` is deleted.
3. **Redaction (T2).** UC2 inherits the platform default and documents no override, but has no test
   proving it. Add one that captures the wire for each vendor leg. If a leg is not redacted, that is
   a finding, not a test to weaken.
4. **Prompt hardening (T1).** Seven `structured()` call sites wrap their input; only two are pinned
   by a test. Pin the rest, and add adversarial items to the UC2 golden set — there are currently
   **none**, so F4 line 11 has never been measurable for this app. Report the success rate; the gate
   is 0%.
5. **Residency (T4).** There is no uc2 sibling to ADR 0014/0017. Write one: which leg carries what,
   in which jurisdiction, at what B5 classification, and what is outstanding (DPA, vendor risk).
6. **Retention (T5).** PRD B5 says "UC2 indefinitely (it's content)" — legitimate, but stated
   nowhere in the repo, so it reads as an omission. Record the N/A and its citation in the README.
7. Write `docs/security/uc2-review.md` in the shape of the other two: T1-T10 and the F4 checklist,
   each MET / PARTIAL / NOT MET / UNMEASURED with evidence (file:line, test name) or the specific
   reason, and an explicit open-gaps list. Grade honestly; a PARTIAL that names its limit is worth
   more than a MET that cannot be checked.

Acceptance: `docs/security/uc2-review.md` exists and every row cites evidence or a reason; the role
matrix is enforced server-side and tested; a spend-scope test fails when the scope is removed; a
redaction test captures the wire; the adversarial subset reports a measured number; an ADR records
both the uc2 residency position and the PRD's missing-prompt omission.

## Execution notes
- All of this is unit-testable with mocked vendors; no Docker and no vendor spend are required
  except the adversarial eval, which needs `INDICAI_ANTHROPIC_API_KEY` (print the estimate first).
- Do not copy uc1's or uc3's grades across. Every claim in the new review must be checked against
  UC2's code; a review that inherits another app's conclusions is the failure mode this prompt
  exists to avoid.
- `quiz_attempts` is the only uc2 table carrying an employee identifier. Say what that means for
  T2, T3 and T5 explicitly rather than leaving a reader to infer it.
