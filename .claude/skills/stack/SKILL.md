---
name: stack
description: Start, check and troubleshoot the local Docker Compose stack (postgres/pgvector, redis, qdrant, minio, TEI bge-m3, sparse sidecar, langfuse, grafana, prometheus, livekit) inside a cloud session or locally. Use when a task needs the database, retrieval, observability, or voice services, or when integration tests or make eval targets fail to connect.
allowed-tools: Bash(make *), Bash(bash scripts/stack.sh*), Bash(docker *), Bash(curl *), Bash(psql *), Bash(redis-cli *), Read
---
# Local stack

Profiles (pick the smallest that covers the task; the VM has 16 GB RAM):
| Command | Services | Use for |
|---|---|---|
| `make stack-core` | postgres, redis, qdrant, minio, tei | migrations, KB ingest, retrieval, most integration tests |
| `make stack-sparse` | core + bge-sparse (port 8081) | hybrid dense+sparse retrieval, `make eval-uc1` with hybrid |
| `make stack-obs` | core + langfuse (3002), grafana (3001), prometheus (9090) | trace/dashboard acceptance criteria |
| `make stack-voice` | core + livekit (7880), livekit-sip | UC1 P5 voice pipeline, `make voice-test` |
| `make up` | everything except the sparse sidecar | full demo |

All ports bind to 127.0.0.1; connection strings come from `.env.stack` (generated, gitignored,
never read by the agent — the apps load it via `indic_platform.config.settings`).

## Cloud-session notes
- The SessionStart hook starts `core` in the background on `startup`/`resume`; check
  `make stack-status` or `.claude/run/stack.log` before assuming it is up. `scripts/stack.sh wait
  tei` blocks until healthy.
- TEI downloads BAAI/bge-m3 (~2.3 GB) on first start into the `tei-data` volume; the environment
  setup script pre-warms it, but a cold environment can take up to 15 minutes. The healthcheck
  `start_period` already allows this. Do not restart it repeatedly.
- The sparse sidecar builds a torch image (~4 GB) and uses ~3 GB RAM. Start it only when hybrid
  retrieval is required by the prompt; stop it afterwards (`docker compose --env-file .env.stack
  --profile retrieval stop bge-sparse`).
- Never `docker compose down -v` (destroys the model cache and data). Plain `down` keeps volumes.
- If `docker info` fails, the daemon is not available in this environment: mark stack-dependent
  criteria UNMEASURED and run the mocked/unit path instead.

## Quick health
```
make stack-status
curl -fsS localhost:6333/healthz && curl -fsS localhost:8080/health && pg_isready -h localhost -p 15433 -U platform
make migrate           # alembic upgrade head against the stack
```
