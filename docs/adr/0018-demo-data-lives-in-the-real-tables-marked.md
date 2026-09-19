# 0018 — Demo data lives in the real tables, marked and excluded from every measurement

- **Status:** Accepted
- **Date:** 2026-09-19
- **Prompt:** none — a demonstration need ahead of `uc3/P8-pipeline`
- **Relates to:** ADR 0005, ADR 0009, ADR 0010, ADR 0011, ADR 0016, PRD E5, E7, E8

## Context

uc3's reviewer console is finished and its queue is empty: nothing connects ingestion to
the detector until `uc3/P8-pipeline`. An empty queue demonstrates nothing, and the
console is the strongest thing this programme has to show.

Filling it means writing rows that no detector produced into `analysis_runs`, `flags`
and `dispositions` — three append-only, hash-chained tables whose entire purpose is that
what is in them can be trusted. Three options were open:

1. **A separate demo database or a parallel schema.** Nothing fabricated ever touches the
   compliance tables. But the demo then exercises a different deployment from the one
   being sold, and the audit chain — the feature the demo most needs to show — is not
   the one under demonstration.
2. **A stubbed API, as the Playwright suite uses.** Truthful and cheap, but it proves
   only that the UI renders a fixture. The queue ordering, the chain, the role checks and
   the metrics are all the parts a compliance buyer asks about, and none of them run.
3. **Real rows in the real tables, marked as demonstration data and excluded from every
   measurement.** What the demo shows is then the actual system.

(3) was chosen. The risk it carries is specific and worth naming: a fabricated row in an
evidence chain is exactly the thing the chain exists to prevent, and a hash chain will
verify a lie as faithfully as it verifies the truth. Hashing does not make a row true.

## Decision

Demo rows are written by `apps/comms_surveillance/demo_seed.py` through
`audit.append` — the same path, the same advisory lock, the same chain — and are
distinguishable from detector output by four independent markers, any one of which is
enough:

- `analysis_runs.model` is `demo-seed` (`demo_seed.py:115`), never a vendor model id;
- `analysis_runs.prompt_version` is empty (`demo_seed.py:121`), because no prompt ran;
- `analysis_runs.output` carries `{"demo": true, "source": "golden:<id>"}`;
- `calls.source_key` is prefixed `demo/uc3/` (`demo_seed.py:112`), which is also the
  uniqueness that makes re-seeding a no-op.

`metrics.DEMO_MARKER` (`metrics.py:316`) is applied by `metrics.demo_free`
(`metrics.py:336`) at **four** sites, and the count matters more than the list: the
first attempt applied it at one, believed that covered everything, and was wrong twice
over.

| surface | where the exclusion lives |
|---|---|
| `/metrics/precision`, by category and over time | `metrics.flag_statement` |
| `/metrics/false_negative_estimate` | `metrics.qa_sample_statement` |
| `/qa-sample`, the lead's stream | `api.queue_statement`, which builds its own join and goes through neither statement above |
| the three Grafana panels reading `flags` | `infra/grafana/dashboards/uc3.json`, raw SQL that computes the same numbers independently of `metrics.py` |

`demo_free` refuses a statement that has not joined `analysis_runs` rather than adding
the join itself: applied to one that has not, the bare `WHERE` is a silent cartesian
product whose compiled SQL still mentions `analysis_runs`, so a test asserting the
predicate is present would pass while every count was multiplied.

The reviewer's queue is the one read path that deliberately keeps demo rows. Showing
them is what they are for.

Both metrics responses return a `demo_excluded` count (`api.py:616`, `api.py:639`)
rather than filtering silently: an operator has to be able to tell an empty dashboard
from one whose every row was removed. It is whole-table where the figures beside it are
windowed, because it answers "does this database hold demo data", which is a property of
the database rather than of the window.

`demo_seed.seed` refuses any `ENV` outside the dev/local/test/ci allow-list, and checks
that itself (`demo_seed.py:671`) rather than in the CLI, because the module ships inside
the production uc3 image.

There is no reset. The app role holds no DELETE on the chained tables (migration
`0008_uc3_audit_role`); a seeder that worked around that would be a seeder that can
rewrite an audit trail. Starting over means a fresh volume.

Two things the seed does **not** fabricate. Cost: `calls.stt_cost_inr` stays zero and
`stt_model` is `demo-seed`, because no transcription happened and an invented rupee
figure would flow straight into the cost panel as if it had been measured. Reviewer
verdicts as evidence: every disposition carries `DEMO_NOTE` (`demo_seed.py:132`) saying
in the row itself that it is neither a real decision nor evidence about precision.

The English beside each non-English evidence span is a real translation, produced once by
`detector.render_english` and checked in as `apps/comms_surveillance/demo/renderings.json`
with the function, model and date that made it, recorded **per span** so a later partial
regeneration cannot relabel older translations. A gloss and a finding are different
claims: the translation is genuine, and the row carrying it is still `demo-seed`. One
Hinglish turn the model handed back unchanged is recorded as untranslatable rather than
stored as its own translation, and the console says no rendering was produced.

## Consequences

- The demo exercises the shipped system: real chain, real queue ordering, real role
  enforcement, real verifier. `uc3.chain_verify` verifies clean over a seeded database,
  anchors included.
- Precision, the false-negative estimate and the QA-sample stream are unaffected by
  seeding. This was not free: before the exclusion, a default 48-call seed produced 33
  flags of which 11 had a decided latest disposition (8 `confirmed`, 3 `false_positive`),
  which the dashboard would have reported as a confirmed/decided ratio of 0.73 — against
  the real measured uc3 precision of 0.30 against a 0.80 gate
  (`docs/build/PROMPT-PLAN.md:41`). That is the passing placeholder CLAUDE.md forbids,
  shown to exactly the audience the demo exists for.
- Any future read path that counts flags or dispositions must apply `DEMO_MARKER` — and
  the four sites above are the evidence that "the metrics exclude it" is not a claim any
  single function can carry. A new aggregate in `metrics.py`, a new route that joins
  `analysis_runs` itself, and a new Grafana panel are each their own obligation. This is
  the weakest part of the decision: the exclusion is a convention enforced by tests
  rather than a property of the schema.
- Retention must not be enabled on a database holding demo data. Both gates are closed
  by default; opened, the sweep would book fabricated deletions into the shared log with
  no demo marker and would strip transcripts from calls whose chained flags quote them.
  Recorded in `apps/comms_surveillance/README.md`.
- A demo database is not a test database. The seeded rows are permanent.

## Evidence

- Seeder and its guards: `apps/comms_surveillance/demo_seed.py`
- Renderings and their provenance: `apps/comms_surveillance/demo_renderings.py`,
  `apps/comms_surveillance/demo/renderings.json`
- Metric exclusion: `apps/comms_surveillance/metrics.py:316-345`,
  `apps/comms_surveillance/api.py:582`, `apps/comms_surveillance/api.py:605`
- The chain still verifies, anchors included:
  `platform/tests/test_uc3_demo_seed.py:548` (`integration`)
- The stored columns say `demo-seed`, and no cost is claimed:
  `platform/tests/test_uc3_demo_seed.py:646` (`integration`)
- The queue shows demo rows and the QA-sample stream does not:
  `platform/tests/test_uc3_demo_seed.py:686` (`integration`)
- Seeding does not move precision or the false-negative estimate:
  `platform/tests/test_uc3_demo_seed.py:711` (`integration`)
- `seed` refuses production itself: `platform/tests/test_uc3_demo_seed.py:192`
- Every measuring query carries the predicate, the queue does not, and the Grafana
  panels do: `platform/tests/test_uc3_demo_seed.py`, the three compiled-SQL tests
- Real measured uc3 precision, for the comparison above: `docs/build/PROMPT-PLAN.md`
