---
paths: ["platform/adapters/**", "platform/security/**", "platform/obs/**", "platform/config/**"]
---
# Adapter and platform rules

- Contracts live in `platform/adapters/base.py`. Do not change a Protocol signature without
  updating every implementation, every app call site, `README.md`'s interface table, and adding a
  test that exercises the new shape with mocked HTTP.
- Streaming Protocol methods are declared with `def` and return async iterators; implementations
  are `async def` + `yield`. Retries happen only before the first emitted chunk.
- All vendor calls go through `platform/adapters/runtime.py` (token bucket, retries with jitter on
  429/5xx max 3, circuit breaker, timeouts: streaming 30s idle, Sonnet 60s, Haiku 20s). Never call
  `httpx`/SDK clients directly from an app.
- Batch submissions carry a content-derived idempotency key; apps still deduplicate durably.
- Redaction (`redact.py`) runs before text leaves for a vendor or a log. Apps that need an
  override (comms_surveillance keeps phone/email as evidence) document it in their README and
  pass an explicit policy object; they do not monkeypatch.
- Cost: every call records units (seconds/chars/tokens) and INR/USD from `config/pricing.yaml`.
  New capabilities need a pricing entry and a test asserting the computed cost.
- Langfuse spans are metadata-only: never trace content, credentials, headers, signed URLs or
  exception messages.
- Verify SDK request shapes with the `sarvam` MCP server or `context7` before finalizing; record
  what was verified in `docs/adapter-verification.md`.
