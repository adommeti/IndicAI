# Helpdesk retrieval (P2)

The FastAPI package exposes the P3 chat surface described below. No redaction
override is enabled; all vendor access goes through `indic_platform.adapters`.

The 25 sample articles describe a fictional employer, not actual corporate policy.
Seven articles explicitly declare the legacy P1 `kb-*` identifiers as aliases;
canonical IDs follow `VPN-001` style. Aliases are metadata, not query heuristics.
Golden labels are unchanged. Native-language editorial review remains pending.

`make ingest-kb` loads the bundled samples. For another folder use
`uv run python -m indic_platform.cli ingest-kb --folder /path/to/kb`.
Markdown requires YAML front matter with `id`, `category`, optional `title` and
`aliases`, plus an H1 title. HTML uses `meta name="article_id"`,
`meta name="category"`, and an H1; scripts/styles/navigation are excluded.

Chunks use the pinned bge-m3 tokenizer, a maximum 500-token budget including
heading hierarchy and special tokens, and 60 body-token overlap within a section.
Short articles remain short; headings are not padded to reach 500 tokens.
UUIDs derive from article, section, offset and content hash. Reingestion upserts
the same IDs and removes only superseded chunks of that article after success.
Run one ingestion writer at a time. Files removed from the input folder do not
silently delete articles already stored. Collection schema mismatches fail closed.

Dense vectors come from the existing TEI server (`BAAI/bge-m3`, 1024 dimensions).
TEI 1.8's `/embed_sparse` requires SPLADE and does not expose bge-m3's lexical
head. The approved sparse sidecar uses the same bge-m3 revision's trained linear
head: ReLU over token hidden states, max positive weight per token ID, excluding
special tokens. It is not BM25 or a second model. See `infra/sparse/README.md`.
Queries remain in their original language. Both branches use Qdrant dense+sparse
RRF; cross-query fusion uses `1 / (60 + rank)` with deterministic chunk-ID ties.
The returned top six are chunks; scores are rank-fusion scores, not probabilities.

`RETRIEVAL__PARALLEL_TRANSLATE=false` is the default. When enabled, Mayura runs
concurrently with original-language search through the redacting Sarvam adapter.
The entire translated branch has a 250ms deadline; timeout/error falls back to
the original results. Timeout cancellation cannot guarantee that upstream work
or billing stopped. At most four translated tasks per retriever may be outstanding;
stuck cancelled work holds its slot, and further optional passes report `busy`
without making vendor calls. Reports estimate attempted translation spend separately from
vendor-confirmed adapter usage. Self-hosted embedding/vector API charges are zero;
the pricing table does not include machine operating costs.

## P3 chat agent

Local Postgres uses port **15433**, avoiding another project's database on 5433.
`make bootstrap` uses this port for new checkouts; existing private `.env.stack`
files must use 15433 in `DATABASE_URL`. Existing data volumes are unchanged.
Run `make migrate`, then serve `helpdesk_agent.api:app` with trusted authentication
middleware. Submit `POST /chat/turn` with `utterance`, `language`, and optionally
the `session_id` returned by a previous turn. `employee_id` and unknown body fields
are rejected. Body values and headers such as `X-Employee-ID` never establish identity.

The deployment's SSO integration must validate its tokens (signature, issuer,
audience and expiry), then populate the ASGI scope through Starlette
`AuthenticationMiddleware` with an authenticated `BaseUser` and `AuthCredentials`
containing the `authenticated` scope. `BaseUser.identity` must be a stable,
issuer-qualified employee subject, at most 128 characters—not a display name.
The API uses this verified identity for both new and resumed sessions; another
employee cannot resume a session merely by knowing its UUID. Without this trusted
middleware, `/chat/turn` returns 401 before model calls or persistence. `/health`
remains public. No IdP-specific token-validation backend is configured by P3;
SSO deployment wiring is still required, with no anonymous development bypass.

The LangGraph workflow retrieves once, calls the cached C7 prompt with
`claude-sonnet-5`, and applies deterministic guards. A rejected decision gets one
retry with guard error codes, then a localized fallback. Ticket actions only log
a fixed stub message and return null ticket_id; no Zammad ticket is submitted.
Postgres row locking serializes turns within each session. Prior prompts contain
only the last eight turns; the clarification cap and grounding evidence use the
whole persisted session. Each successful turn stores decision, retrieval, stage
timings, model, policy/prompt hash, and its Langfuse trace ID. Trace metadata omits
conversation content. Exhausted transient Claude failures use a localized ticket
offer without another vendor retry. Permanent vendor errors (including billing)
and infrastructure failures propagate and roll back the transaction.

`make eval-uc1` now runs all 150 text golden items through the real agent and
enforces P3 action, reply-language, and adversarial gates. Speech/groundedness
gates remain explicitly unmeasured. The runner accepts synchronous and async
decision hooks. Reports include failure reasons per item. The old P2 evaluation
remains available with `python -m indic_platform.eval.runners.run_uc1 --baseline
--retrieval --compare-translate`. Roman Hindi identification uses a deterministic
Hindi-marker heuristic because langid does not identify transliteration reliably;
native scripts use unrestricted langid classification.

Decision references expose only `article_id`, title, and text; internal chunk IDs
and retrieval aliases remain in the retrieval audit, avoiding ambiguous citation
choices. The cached supplement distinguishes requests for instructions from vague
malfunctions and requests for human action. A retry includes exact allowed article
IDs and the verifier's unsupported claims in escaped, untrusted diagnostic tags.
The grounding verifier and citation guard remain fail-closed.

Roman Hindi also recognizes common transliterated verb forms, while rejecting
native Indic script in a Roman-script reply. This remains a heuristic, not a
general transliteration language model. Regression tests cover valid imperative
replies, English negatives, mixed-script negatives, and escaped retry diagnostics.

Use `make eval-uc1 UC1_EVAL_ARGS='--concurrency 3 --output /tmp/uc1-eval'` to run
independent golden sessions concurrently and retain a separate report. The default
remains serial; with concurrency, other in-flight calls may finish if one fails.

### Ticket grounding

The approved C6 revision removes the 40-word minimum and literal cross-language
word overlap. Descriptions may be concise English summaries of employee statements.
A separate Sonnet 5 structured call checks every factual claim in the title and
description against employee statements, including negation and uncertainty. It
receives no tools, articles, or assistant replies as factual evidence. The C7 base
prompt stays verbatim, with a cached, versioned grounding supplement appended.

The guard requires an approving verdict bound to the exact candidate and source
hashes, nonempty verbatim source quotes, and no unsupported claims. It fails closed
on missing, malformed, or unavailable verification. Model judgment can still be
wrong; deterministic quote matching proves provenance, not semantic entailment.
The additional verifier call adds latency and model cost for ticket candidates.
Generated titles and summaries omit raw phone numbers, email addresses and national
IDs. Candidates containing values detected by the platform redactor are rejected
before verification, so different values cannot collapse into the same redaction
placeholder and receive false approval. Original values remain in local evidence.

After the one allowed retry, a capped session gets an exact application-owned
English human-review notice, not an unverified translated description. Model output
cannot bypass verification by copying the template; only the internal fallback
path may use it without a semantic verdict. Fallback category is the generic IT
queue pending human triage. It has `review_required=true` in the audit.

New turns preserve original employee utterances locally. `decision_json._grounding`
stores the evidence, verifier model/prompt version, verdicts, hashes, and review
status; `_guard_errors` records rejection codes. Older redacted utterances cannot
be reconstructed. Raw evidence is never exported to traces; adapters redact all
vendor-bound text. Decision prompt and combined guard/verifier policy versions are
stored separately. The public Ticket schema is unchanged. Golden labels are unchanged.

The live Sonnet 5 API rejects `temperature` as deprecated. The shared Claude
adapter omits it for this exact model and retains temperature 0 for older models.
This is an API compatibility exception to the PRD, not a guarantee of deterministic
sampling. Cached system prompts and pydantic structured output remain enabled.
Reference: Anthropic Python SDK `MIGRATION.md` (sampling parameter migration).

Local verification: `make lint typecheck test`. With the migrated Postgres and
Langfuse services running, `LIVE_API_TESTS=1 uv run pytest
platform/tests/test_helpdesk_agent.py -m slow -q -s` tests real persistence and
trace availability with mocked model/retrieval calls. It leaves synthetic test
sessions in the local database.

## Ticketing backend (P4)

Ticket filing targets a self-hosted [Zammad](https://zammad.org). It is not part
of `make stack-core`: it lives behind the compose profile `ticketing` and starts
only with `make stack-ticketing`, which brings up eight containers pinned to
these tags — `ghcr.io/zammad/zammad:7.1.3-0014` (running four roles: `init`,
`railsserver`, `nginx`, `scheduler`, `websocket`), `postgres:17.11-alpine`,
`redis:8.10.1-alpine` and `memcached:1.6.45-alpine`. Zammad brings its own
Postgres, Redis and memcached; none of them touches the platform's `postgres`
and `redis` services, and no compose profile other than `ticketing` references
them. `make down` now tears this profile down alongside `retrieval`.

What it costs to run: the four images are about 500 MB of compressed download
(measured from the registry manifests; more once unpacked) and the first start
also runs Zammad's database migrations, so budget several minutes before the
API answers. Four Rails containers stay resident afterwards. Actual memory and
CPU use have not been measured for this build — assume it is the heaviest
optional profile in the stack and do not run it next to `stack-obs` on a small
machine without checking.

The service set is transcribed from upstream `zammad/zammad-docker-compose`
(same tag) with two changes: the `zammad-elasticsearch` service is dropped and
`ELASTICSEARCH_ENABLED=false` is set, which is upstream's documented way to run
without Elasticsearch; and upstream's pass-through environment variables are not
reproduced, so anything beyond what `docker-compose.yml` sets has no effect here.
Note that the variable is `ELASTICSEARCH_ENABLED`, not the
`ZAMMAD_ELASTICSEARCH_ENABLED` named in the prompt — the latter is not a Zammad
variable. Without Elasticsearch, Zammad falls back to database search, which is
weaker; nothing in uc1 depends on Zammad's search. `zammad-nginx` publishes on
`127.0.0.1:8082` because 8080 and 8081 are taken by `tei` and `bge-sparse`.

The compose file holds no password. Zammad's database credentials come from
`${STACK_PASSWORD}`, the same private, generated `.env.stack` value the rest of
the stack uses (`make bootstrap` creates it; it is never committed). `ZAMMAD_URL`
and `ZAMMAD_TOKEN` are read from the environment and are listed empty in
`.env.example`, with the steps for minting a token.

### Seeding

Zammad's first-run web wizard (admin user, organisation) is manual; it is not
scripted here. Once it is done and a token exists:

```
ZAMMAD_URL=http://localhost:8082 ZAMMAD_TOKEN=... \
    uv run python -m helpdesk_agent.zammad_seed
```

This creates the group **IT Support** and the Ticket attribute
**`source_session_id`** (plain text, 64 chars), then applies Zammad's pending
object-attribute migrations. It is idempotent by check-before-create: it lists
`/api/v1/groups` and `/api/v1/object_manager_attributes` first, prints for each
object whether it created it or found it already there, and treats Zammad's own
422 on a duplicate as "already exists" so two seeders racing cannot both create.
The migration step is run every time because it is a no-op when nothing is
pending, which lets a run interrupted halfway converge on the next run. Running
it twice produces one group and one field. It is an operator CLI rather than a
request path, so it uses `httpx` directly instead of `indic_platform.adapters`,
which exist to wrap the two model vendors.

### What employee data reaches Zammad

A filed ticket is a support ticket containing an employee's own words, and
Zammad is a separate system with its own database, its own operators and its own
retention. Every ticket this agent files carries, out of the agent and into
Zammad:

- the **ticket title** — a short generated summary of the employee's problem;
- the **ticket description** — a generated summary of what the employee said,
  which is about their words even when it is not a verbatim quote;
- the **`source_session_id`** — the helpdesk session UUID, which links the ticket
  to the stored conversation and is therefore an identifier for that employee's
  session, not an anonymous value;
- the ticket's **category** and **urgency** (`IT`/`HR`/`Facilities`,
  `low`/`normal`/`high`), and the fixed tags `voice-agent` and `auto-filed`;
- the **employee identifier** the ticket is filed on behalf of, so the ticket is
  attributable to a named person inside Zammad.

Raw utterances, audio and retrieval evidence stay in the platform's own
Postgres and are not sent. Titles and descriptions go through the same grounding
and redaction path as the rest of the agent: candidates containing phone
numbers, email addresses or national IDs detected by
`indic_platform.security.redact` are rejected before a ticket is created, so
those values are not expected to reach Zammad. That is a detector, not a proof —
treat the Zammad instance as holding employee personal data and scope its access
and retention accordingly.
