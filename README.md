# Indic AI platform — P0 scaffold

Python 3.12 / uv workspace, three FastAPI app skeletons, shared vendor adapters,
security utilities, database migrations, offline evaluation reports and local infrastructure.
Application workflows and UIs are implemented by the subsequent UC prompts.

## Start

1. Install uv, Docker Compose, and ffprobe (FFmpeg; required for non-WAV media billing).
2. Copy `.env.example` to `.env` if needed and supply the two vendor API keys.
3. `uv sync --frozen --all-packages`
4. `make up` — generates `.env.stack` with random credentials and waits for all healthchecks.
5. `make migrate`
6. `make lint typecheck test eval-uc1 eval-uc2 eval-uc3`
7. `LIVE_API_TESTS=1 uv run pytest -m slow` — one live smoke per vendor, with local cost traces.

The existing `.env` is preserved. Local `.env.stack` overrides stack connection settings,
including old conflicting ports, without replacing vendor API keys. Both files are ignored.
Treat `.env.stack` as persistent local configuration: do not regenerate it while retaining
initialized database volumes. `make down` preserves volumes. Docker needs approximately
8 GB RAM for this reduced-batch configuration; other workloads can increase that requirement.
TEI uses amd64 CPU emulation on Apple Silicon, BAAI/bge-m3, and truncates to 512 tokens to
fit the local VM. Increase batch capacity before evaluating long-document retrieval.

Langfuse: http://localhost:3002 (user `admin@localhost.test`, password `STACK_PASSWORD`
from `.env.stack`). Grafana: http://localhost:3001 (`admin`, same local stack password).
Traces contain metadata only: vendor/model, prompt version, latency, usage and INR/USD cost.
No content, credentials, request headers, signed URLs or exception messages are traced.

## Build workflow

The remaining work is an ordered prompt series under `prompts/`, tracked in
[docs/build/PROMPT-PLAN.md](docs/build/PROMPT-PLAN.md) and executed one PR per prompt:
`bash scripts/run-prompt.sh <uc> <Pn>` starts a prompt on a fresh branch, `make check` is the
quality gate, and `bash scripts/ship.sh` pushes, opens the PR, waits for CI and squash-merges.
Operating procedure for cloud sessions: [docs/build/RUNBOOK.md](docs/build/RUNBOOK.md).
Working agreement for the coding agent: [CLAUDE.md](CLAUDE.md).

## Architecture decisions

Program-level decisions and their consequences are recorded as ADRs, indexed in
[docs/adr/README.md](docs/adr/README.md): reasoning in the user's language (0001), one platform
package behind three apps (0002), hybrid hardened detection for surveillance (0005), meeting
minutes as a separate consented module (0006), the build workflow (0007) and the cloud build
environment contract (0008). ADRs 0003 (dubbing contract) and 0004 (surveillance scope) are
reserved for the prompts and the governance gate that own them. `docs/prd-v2.md` is the
specification and is read-only; deviations from it are recorded as ADRs.

## Package and interfaces

Source lives in `platform/`; import it as `indic_platform` to avoid shadowing Python's
stdlib `platform`. Apps import vendor functionality only from these adapters.
See [contracts](platform/adapters/base.py) and [SDK verification](docs/adapter-verification.md).

| Contract | Concrete adapter | Methods |
|---|---|---|
| STT | `SarvamSTT` | `stream(audio, language="auto")`, `batch(audio_uri, language="auto", diarize=False)` |
| TTS | `SarvamTTS` | `stream(text_chunks, language, voice)`; REST convenience `speak(...)` |
| Translate | `SarvamTranslate` | `translate(text, source="auto", target, mode="formal")` |
| Dubbing | `SarvamDubbing` | `submit(video_uri, target_languages, voice_map)`, `status(job_id)`, `fetch(job_id)` |
| LLM | `Claude` | `structured(system, user, schema, model, cache_system=True)`, `stream_text(system, messages, model)`; constructor takes `redactor` and `wrapper` so an app can document an evidence-preserving override (PRD E9) |
| VectorStore | Protocol for UC1 retrieval | `search(vector, limit=3, filters=None)` |

Streaming Protocol methods return asynchronous iterators directly (declared with `def` in
Protocols, `async def` plus `yield` in implementations), avoiding an extra coroutine layer.
STT input is 16 kHz mono PCM16; streaming times are receive-window estimates because Saaras
streaming has no timestamps. TTS accepts whole sentences and yields MP3 frames. Batch media
must be materialized to a local path or `file://` URI; cloud-object fetching belongs to app
storage integration. Dubbing supports the SDK's single `voice_id` per job and rejects a
multi-voice map explicitly. It disables voice cloning. Poll intervals cap at 30s; STT batch
deadline is 600s. Submitted duration is used for estimated STT/dubbing billing, charged once
at successful start; it is not a vendor invoice reconciliation.

All adapters default to one shared process token bucket (1000 rpm with a one-request burst).
Inject `RedisTokenBucket` with a common account key into runtimes to share the limit across
Celery/app processes. Circuit breakers expose chat-only, text-only, and queue/template flags;
applications consume these flags when implementing their workflows. Stream retries occur
only before the first emitted output, preventing duplicated audio/text after partial delivery.
Batch request headers carry stable content-derived idempotency keys; vendor enforcement of
those headers has not been established, so durable application deduplication is still required.

## Services and full published port map

All bindings are on `127.0.0.1`; ranges below are inclusive. Container-internal ports remain
standard. Langfuse connects to `langfuse-postgres:5432` on the Compose network.

| Service | Host → container | Protocol |
|---|---|---|
| postgres (pgvector/PostgreSQL 16) | 15433 → 5432 | TCP |
| redis | 6380 → 6379 | TCP |
| qdrant | 6333 → 6333; 6334 → 6334 | TCP |
| minio | 9000 → 9000; 9001 → 9001 | TCP |
| livekit | 7880 → 7880; 7881 → 7881 | TCP |
| livekit | 50100–50120 → 50100–50120 | UDP |
| livekit-sip | 5060 → 5060 | TCP and UDP |
| livekit-sip | 50200–50220 → 50200–50220 | UDP |
| tei | 8080 → 80 | TCP |
| langfuse | 3002 → 3000 | TCP |
| grafana | 3001 → 3000 | TCP |
| prometheus | 9090 → 9090 | TCP |
| langfuse-postgres | none (internal 5432) | TCP |
| langfuse-worker | none (internal 3030) | TCP |
| clickhouse | none (internal 8123/9000) | TCP |

`make logs` tails this project's services; no existing projects are stopped or reconfigured.
Prometheus scrapes itself, Qdrant and LiveKit. App metric serving, dashboards, KB ingestion,
voice routing and UIs are subsequent application work. `make ingest-kb` exits with an explicit
prerequisite message until the UC1 corpus/pipeline exists. `make voice-test` runs Sarvam's
live smoke; the command above runs both vendors.

## Evaluation and boundaries

Each offline eval target checks eight synthetic utility fixtures (100% evidence checking,
delimiter integrity, and redaction). These are not B6 application-quality or model-injection
scores. Full golden sets and B6 go/no-go measurements are P1/application work. See
[golden conventions](platform/eval/README.md).

P0 provides hardening primitives, metadata-only traces and configurable controls. It does
not implement app SSO, retention, audit-chain governance, per-session/day spend caps, or
cloud deployment. Those remain the relevant app/P7 work; this scaffold does not claim the
full F4 security checklist is complete.
