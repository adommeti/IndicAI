---
paths: ["apps/**"]
---
# Application rules

- Apps import vendor functionality only from `indic_platform.adapters`. No `sarvamai`, `anthropic`
  or `langchain_*` imports in `apps/`.
- Every app has: `api.py` (FastAPI), a workflow module (`graph.py` / `pipeline.py` / `detector.py`),
  `prompts/*.md` (versioned; `prompt_version` = sha256[:12] of the file content, computed at load),
  `tests/` (mocked vendors; `integration` marker for stack-dependent tests), and a `README.md`
  that states data classification, retention, redaction policy and any override.
- Prompts ship verbatim from `docs/prd-v2.md` where the PRD provides text (C7, D7, E6). Improve
  behaviour with guards, retrieval, and schema — not by drifting the prompt text silently. A
  prompt change is a versioned change with an eval run attached.
- Structured outputs: `platform.adapters.claude.Claude.structured(...)` with a pydantic schema,
  temperature 0, cached system prompt. Guard nodes are deterministic (no LLM).
- Side effects (tickets, dubbing jobs, uploads) are idempotent per `(session_id, turn_index)` or
  `(module_id, language, stage, version)` and safe to retry.
- Persisted rows record `model`, `prompt_version`, `policy_version`/`lexicon_version` where the PRD
  says so. Timestamps are timezone-aware UTC.
- Auth: identity from the SSO claim (dev bypass flag `AUTH__DEV_BYPASS=true` sets a fixed test
  identity and is refused when `ENV=prod`). Roles are enforced server-side; UI hiding is not access
  control.
- Language handling: reply in the user's language and script; unsupported language → English +
  logged `unsupported_language` event. Never route conversation through translation unless the
  PRD's B2 table says so for that path.
