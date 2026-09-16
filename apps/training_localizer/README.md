# training_localizer

Localizes approved English compliance-training modules into Hindi, Telugu and
Tamil (PRD D4–D7). All vendor access goes through `indic_platform.adapters`; no
`sarvamai` or `anthropic` import appears in this package.

**Data classification.** Module scripts are internal, non-personal corporate
training content. The material risk is an *incorrectly localized obligation*,
not a privacy breach, which is why LOCKED statements, post-edit enforcement,
back-translation QA and a human gate are all in the path.

**Redaction.** No override is enabled: `indic_platform.security.redact.redact`
runs inside the adapters before any text reaches a vendor or a log. Scripts are
wrapped with `wrap_untrusted` and labelled as data in every prompt, per D8 —
an uploaded script is untrusted input even though it comes from a colleague.

**Retention.** `localizations` is append-only in practice: a stage re-run writes
a new `version` rather than overwriting, so the full editorial history of a
module is the audit record of what was delivered. Nothing here is deleted by a
retention job yet; `uc2/P5` owns delivery and `quiz_attempts`, which is the first
table in this app to hold an employee identifier.

## Pipeline (P2)

`pipeline.py` exposes five Celery tasks in PRD D5 order:

    adapt -> translate -> post_edit -> backtranslate_qa -> quiz -> (human gate)

Each reads the **newest version** of the stage before it and writes `version + 1`
of its own, keyed `(module_id, seg_id, language, stage, version)`. That is what
makes a stage independently re-runnable: re-running `post_edit` after a glossary
change does not re-translate, and a reviewer edit triggers re-production rather
than re-translation.

The stage logic lives in `stages.py` as pure functions with the vendor call
injected, so every stage has a unit test with no network and the eval runner can
drive the real stages against the golden set before any module is uploaded.

### adapt and the timing budget

`adapt` rewrites English, but what has to fit the segment is the *translation*.
The budget is therefore an English word count scaled by a measured per-language
rate in `platform/config/timing.yaml` (`words_per_second`: hi 1.93, te 1.96,
ta 1.85, derived from the golden references; reproduce with
`uv run python -m indic_platform.eval.runners.run_uc2 --words-per-second`).
The golden scripts were authored at about 2.5 English words/second, so adapt has
to remove roughly a quarter of the words.

The D7 prompt asks the model to respect the budget; the stage enforces it. A
segment still over budget is re-sent **once** with a shorten hint naming the
exact word count. A segment still long after that retry is returned unchanged
and listed in `meta["over_budget"]` — the pipeline reports an overrun rather
than truncating a compliance obligation mid-sentence. LOCKED segments never go
to the model at all.

### post_edit: Claude proposes, the enforcer disposes

PRD D7 puts the 100% terminology-adherence gate in `post_edit`, a Claude call. A
stochastic model cannot *guarantee* a hard gate, so the stage has two halves:
Claude does the part needing judgement (re-wording so an approved term reads
naturally), then `terminology.enforce` makes the result satisfy the glossary
unconditionally. Enforced repairs are appended to the same change log with
`enforced: true`, so a reviewer sees which edits came from the model and which
were imposed.

`enforce` alone already satisfies the gate
(`test_enforce_alone_meets_the_gate`), which is why terminology adherence was
measurable at 100% in a session with no Anthropic key.

**Known cost of that guarantee.** When a required term is missing entirely there
is no wrong text to swap out, so the enforcer *appends* the required form rather
than dropping the obligation. A clumsy sentence is caught by the fidelity judge
and by the human reviewer; a silently missing obligation is not. On the measured
golden run, 59 of 180 segment-languages took at least one append, 82 in total,
and almost every one was a `keep_english` term Mayura had translated away —
`password` 18 times, `email` 12, `helpdesk` 6, then `phishing`, `MFA`, `vishing`,
`laptop`, `SharePoint`, `Teams`, `DLP`. Those appends are the main reason the post-edited
text is longer than the raw translation, and `uc2/P3`'s reviewer UI is where they
get repaired into the sentence.

### API

- `POST /modules` — title plus timestamped segments; `video_uri` references an
  object already in MinIO rather than carrying bytes. A segment marked `locked`
  whose English does not match an approved statement is rejected with 422: a
  locked segment with no approved rendering has no defined target text.
- `POST /modules/{id}/localize?languages=hi-IN,te-IN,ta-IN` — queues the chain,
  returns 202 with a task id.
- `GET /modules/{id}/status` — per-language stage versions, segment counts,
  flagged segments and fidelity mean where measured.

Authentication is the same fail-closed SSO contract as `helpdesk_agent`:
identity comes from trusted middleware on the ASGI scope, never from a body
field or header, and a bare deployment refuses every write.

## Terminology

`terminology/glossary.yaml` and `terminology/approved_renderings.yaml` are
versioned in git; `load_glossary` derives a content hash that every
`localizations` row records in `meta.glossary_version`. **Both files are still
`status: draft`** and need a bilingual reviewer before `uc2/P3` treats them as
binding — see the `uc2/P1` report.

## Running it

Unit tests need nothing: `uv run pytest platform/tests/test_uc2_pipeline.py`.
The versioning test needs Postgres:
`make stack-core && make migrate && uv run pytest platform/tests/test_uc2_pipeline.py -m integration`.
CI runs it on every push (the `integration (postgres · redis · qdrant)` job), which
is where the D5 promise — re-running `post_edit` leaves `translate` at its old
version — is actually verified, along with `alembic upgrade head && alembic check`
against PostgreSQL 16.
`LIVE_API_TESTS=1 uv run pytest platform/tests/test_uc2_pipeline.py -m slow` runs
one segment through **translate and post_edit** against the real vendors; it does
not cover adapt, backtranslate_qa or quiz, and post_edit's model leg only runs
when an Anthropic key is set (`INDICAI_ANTHROPIC_API_KEY`, or `ANTHROPIC_API_KEY` outside a Claude Code session).

Three eval targets, because only one of them is free:

| target | what it runs | cost |
|---|---|---|
| `make eval-uc2` | the uc2/P1 trivial baseline, no vendor calls — this is what CI runs | free |
| `make eval-uc2-sarvam` | translate + deterministic post_edit over the golden set | ~₹28 |
| `make eval-uc2-live` | the whole pipeline, adapt and the judge included | ~₹110 |

The two vendor targets pass `--fidelity-source sut`, which points the judge at
this pipeline rather than at uc2/P1's draft references, and `--pre-edit`, which
scores the translate stage *before* post_edit. That second number is the one
that can fail: `enforce` implements exactly the predicate the scorer tests, so
`terminology_adherence` on its own reaches 1.0 for any translator at all. Both
print a cost estimate before spending anything.
