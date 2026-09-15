# Architecture decision records

One file per decision, numbered, never rewritten in place: a decision that changes gets a new ADR
whose Status supersedes the old one, and the old one is marked `Superseded by ADR-NNNN`. The
specification (`docs/prd-v2.md`) is read-only; a deviation from it is recorded here.

Start from `0000-template.md`. Every ADR carries Status, Context, Decision, **Consequences** and
**Evidence** — the last being `path:line` links to the code, tests or measurements that implement
or verify the decision. Anything not yet built is written as "not implemented — owned by
`<group>/<Pn>`", never implied to exist, and a measurement that has not been taken is
"unmeasured", never a placeholder number.

| ADR | Decision | Status | Owner |
|---|---|---|---|
| [0000](0000-template.md) | Template | — | — |
| [0001](0001-reason-in-users-language.md) | Claude reasons in the user's language; translation only for English artifacts and as an optional retrieval booster | Accepted | `program/P0`, applied by `uc1/P2`–`P3` |
| [0002](0002-one-platform-three-apps.md) | One shared platform package, three thin apps, one Compose stack; adapters are the only vendor boundary | Accepted | `program/P0` |
| [0003](0003-dubbing-contract.md) | Dubbing takes a caller-supplied script via `srt_upload_url`, so UC2 uses path (a) | Accepted | `uc2/P0-spike` |
| 0004 | Surveillance scope: perimeter, lawful basis, retention, access | **Reserved** | Gate 0 (Compliance/Legal/HR), PRD E2 |
| [0005](0005-hybrid-detection-hardened-analysis.md) | Hybrid lexicon + LLM detection with tool-less, verified, canaried analysis | Accepted (design) | `uc3/P1` (adversarial set), `uc3/P3`–`uc3/P5` |
| [0006](0006-meeting-minutes-separate-module.md) | Meeting minutes is a separate, separately-consented module | Accepted | — |
| [0007](0007-build-workflow.md) | One PR per build prompt, squash-merged, gated by three CI jobs and three verification tiers | Accepted | `program/P0` |
| [0008](0008-cloud-build-environment.md) | The cloud build environment is a contract held in the repository | Accepted | `program/P0` |
| [0009](0009-instruction-flag-evidence-exemption.md) | Instruction-like content is flagged without a verbatim evidence span, and that exemption is bounded and measured | Accepted | `uc3/P4` |
| [0010](0010-audit-chain-order-and-timestamps.md) | The audit chain walks a `seq` identity column; `created_at` is application-set | Accepted | `uc3/P5` |
| [0011](0011-audit-chain-tamper-evident-not-non-repudiable.md) | The audit chain is tamper-evident, not non-repudiable; a trust root outside the database is deferred | Accepted | `uc3/P5`, notarisation in `program/P10` |
| [0012](0012-multiple-alembic-revisions-in-uc3-p5.md) | Four alembic revisions in one prompt, split by rollback semantics | Accepted | `uc3/P5` |
| [0013](0013-governance-role-is-subtractive.md) | The `governance` role subtracts transcript and audio access rather than granting a subset | Accepted | `uc3/P6` |
| [0014](0014-uc1-cross-border-claude-leg.md) | UC1 accepts the cross-border Claude leg for redacted text only; audio never leaves India, and real data waits on a DPA | Accepted | `uc1/P7` |

## Reserved number

`0004` is deliberately unwritten. It cannot be decided from this repository:

- **0004 — surveillance scope.** Requires a written answer from Compliance, Legal and HR to the
  Gate 0 questions in `docs/prd-v2.md` E2. Nothing in UC3 touches real data until it exists;
  `uc3/P1` proceeds synthetic-only and says so, and `uc3/P7` leaves the retention deletion job
  disabled until this ADR sets the rule.

Do not write it from inference. An ADR that guesses at a lawful basis is worse than an absent one,
because the prompts downstream treat it as settled.

## Where the decisions come from

`docs/prd-v2.md` G1 is the decision log these records expand: 0001–0006 are listed there with
their one-line consequence. 0007 and 0008 are not in G1 — they were forced by the build method
(autonomous cloud sessions) rather than by the product design, and are recorded here so the
method is as reviewable as the architecture. 0009–0012 are likewise absent from G1: they
record where an implementation had to deviate from the PRD's own text, and 0011 in
particular bounds a security claim the PRD states more confidently than the code earns.
