# ADR 0008 — The cloud build environment is a contract, held in the repository

- **Status:** Accepted
- **Date:** 2026-09-15 · **Owner:** `program/P0`
- **Relates to:** ADR 0007 (the workflow that runs in it), ADR 0002

## Context

The build runs as unattended Claude Code sessions in an ephemeral cloud container: the repository
is cloned fresh, the container is reclaimed afterwards, and nothing a session installs survives
except what it commits. A session that has to discover its own environment burns its budget on
setup and fails in ways that look like code failures — a blocked host reads as a vendor outage, a
cold model download as a hung stack.

The environment therefore has to be a written contract. The half that lives outside git (the
cloud environment's network allowlist and variables) must be reproducible from something that is
in git, or the next environment is configured from memory.

## Decision

The environment contract is `docs/build/RUNBOOK.md` §1, and its executable half is
`scripts/cloud-setup.sh`, pasted verbatim into the environment's setup-script field. Four terms:

**1. Setup script.** Runs as root before Claude Code starts, on the first session of a new
environment cache; its results are snapshotted and reused. It installs `ffmpeg` and `shellcheck`,
runs `uv sync --frozen --all-packages`, generates `.env.stack`, pulls and builds the Compose
images, and warms the `bge-m3` weights into the `tei-data` volume. It must finish in ~5 minutes
and exit 0 — every step is failure-tolerant, because a setup failure must not block the session.

**2. Network allowlist.** Default package managers, plus: `api.sarvam.ai`, `docs.sarvam.ai`,
`huggingface.co` and its CDN/`*.hf.co` hosts (TEI weights), `ghcr.io`, `github.com`,
`objects.githubusercontent.com`, `mcr.microsoft.com`, `aka.ms`. Anything not on it fails closed.

**3. Environment variables.** `SARVAM_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`,
`LIVE_API_TESTS=0`, and the commit identity `INDICAI_GIT_NAME` / `INDICAI_GIT_EMAIL`. Keys are
environment variables, never files in the repository; `.env` and `.env.stack` are gitignored and
the agent never reads or edits them. `.env.example` lists every variable. The Anthropic key must
be a variable (the proxy attaches no credentials to `api.anthropic.com`); the Sarvam key may
instead be a host credential for `api.sarvam.ai`, with the variable left as a placeholder so the
SDK client still constructs. A missing key is not a workaround: live steps are skipped and
reported blocked. The identity variables are the exception to placeholder tolerance — set to
placeholder text they override `_lib.sh`'s correct default and fail every commit's attribution.

**4. Memory budget: 16 GB, spent through profiles.** The stack is never started whole. The
SessionStart hook starts `core` (postgres, redis, qdrant, minio, tei — ≈ 5 GB) in the background;
a prompt adds `obs`, `voice` or `sparse` only when it needs them. `sparse` is memory-heavy and has
a native fallback for when Docker cannot hold it.

## Consequences

- **Positive:** a new environment is reproducible from the repository — the runbook table plus one
  pasted script — rather than from whoever configured the last one.
- **Positive:** sessions start warm. The expensive, cacheable work (dependency resolution, image
  pulls, the `bge-m3` download) happens once per environment cache, not once per session.
- **Positive:** the allowlist is a real boundary: adding a vendor host is a visible change.
- **Negative:** the snapshot expires (~7 days) and the setup budget is ~5 minutes. If TEI weights
  are still downloading at session start, `scripts/stack.sh wait tei` blocks until healthy — which
  a session must expect rather than diagnose as a broken stack.
- **Negative:** 16 GB means no session verifies the whole system at once. Prompts needing voice,
  observability and sparse retrieval must sequence their profiles, and the plan's `verify` column
  has to be honest about which tier actually ran.
- **Negative:** the allowlist and variables live in the cloud console, so the repository copy can
  drift from reality. The runbook table is the source of truth; a host added in the console
  without a runbook change is a defect.
- **Negative:** Docker is not guaranteed; a session can start with the daemon unavailable. When it
  is absent, stack-dependent criteria are reported UNMEASURED, never approximated.

## Evidence

- `scripts/cloud-setup.sh` — the pasted setup script, including the TEI weight warm-up and its
  failure-tolerant structure.
- `docs/build/RUNBOOK.md` §1 — the environment table (name, network access, variables, setup
  script) and the notes on key and identity placement; §7 — the troubleshooting table for a blocked
  host, an unhealthy TEI, a missing Docker daemon and a placeholder identity variable.
- `.env.example` — every variable the platform reads; `.gitignore` — `.env`, `.env.stack`.
- `scripts/stack.sh:14-27` — the `core` / `obs` / `voice` / `sparse` profiles and `wait`;
  `infra/sparse/README.md` — the native fallback when Docker lacks RAM.
- `.claude/hooks/session-start.sh` — reports key presence and Docker availability at session
  start, and starts `core` in the background; `infra/bootstrap.py` — generates `.env.stack`.
