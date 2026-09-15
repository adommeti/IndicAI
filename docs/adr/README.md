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
| 0003 | Dubbing API contract and the UC2 production path (D9 a or b) | **Reserved** | `uc2/P0-spike` |
| 0004 | Surveillance scope: perimeter, lawful basis, retention, access | **Reserved** | Gate 0 (Compliance/Legal/HR), PRD E2 |
| [0005](0005-hybrid-detection-hardened-analysis.md) | Hybrid lexicon + LLM detection with tool-less, verified, canaried analysis | Accepted (design) | `uc3/P1` (adversarial set), `uc3/P3`–`uc3/P5` |
| [0006](0006-meeting-minutes-separate-module.md) | Meeting minutes is a separate, separately-consented module | Accepted | — |
| [0007](0007-build-workflow.md) | One PR per build prompt, squash-merged, gated by three CI jobs and three verification tiers | Accepted | `program/P0` |
| [0008](0008-cloud-build-environment.md) | The cloud build environment is a contract held in the repository | Accepted | `program/P0` |

## Reserved numbers

`0003` and `0004` are deliberately unwritten. Neither can be decided from this repository:

- **0003 — dubbing contract.** Requires live Sarvam Dubbing API responses. `uc2/P0-spike` writes
  it, with request/response snippets under `docs/adr/assets/0003/`, and `uc2/P1` and `uc2/P4`
  read it. Until it exists, the UC2 production path is undecided.
- **0004 — surveillance scope.** Requires a written answer from Compliance, Legal and HR to the
  Gate 0 questions in `docs/prd-v2.md` E2. Nothing in UC3 touches real data until it exists;
  `uc3/P1` proceeds synthetic-only and says so, and `uc3/P7` leaves the retention deletion job
  disabled until this ADR sets the rule.

Do not write either from inference. An ADR that guesses at a vendor contract or a lawful basis is
worse than an absent one, because the prompts downstream treat it as settled.

## Where the decisions come from

`docs/prd-v2.md` G1 is the decision log these records expand: 0001–0006 are listed there with
their one-line consequence. 0007 and 0008 are not in G1 — they were forced by the build method
(autonomous cloud sessions) rather than by the product design, and are recorded here so the
method is as reviewable as the architecture.
