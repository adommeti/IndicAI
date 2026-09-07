# Adapter verification — 2026-09-07

Versions resolved in `uv.lock`: sarvamai 0.1.32, anthropic 1.4.0, langfuse 3.15.0.
Context7 consulted the official Sarvam cookbook, Anthropic Python SDK and Langfuse docs.
Request construction was checked against installed generated SDK definitions, then exercised
with mocked HTTP through the official clients (Anthropic 1.4 uses httpx2).

| Capability | Verification |
|---|---|
| Translation | MCP `/translate` reference + live Mayura call; live adapter smoke passed |
| REST TTS | MCP `/text-to-speech` reference + live Bulbul call; HTTP adapter fixture |
| Streaming TTS | MCP stream call + live adapter stream passed (5 chunks); socket cleanup tests |
| STT | MCP `/speech-to-text` reference + live synthetic Hindi WAV transcription |
| Batch STT | MCP legacy `/speech-to-text/job/init` reference and live MCP job; SDK uses `/speech-to-text/job/v1`; live adapter diarized job passed |
| Dubbing | MCP audio dubbing pipeline passed; video jobs verified against SDK create/upload/start/live-status/export-status definitions and mocked HTTP |
| Claude | Context7 + official SDK; mocked structured output and SSE; live request rejected for insufficient account credits |

The MCP batch helper returned a Completed job with an empty transcript; the SDK adapter
separately downloaded its output and produced one transcript segment successfully.
MCP TTS examples use `inputs`/`target_language_code`, while this pinned SDK's REST method
uses `text`/`language_code`; tests assert the actual SDK serialization.
The MCP dubbing tool performs audio STT/translation/TTS and cannot verify the separate video
job endpoint. A real video job was not submitted. No endpoint shape is inferred from that
MCP audio result. The SDK cannot implement arbitrary per-speaker voice mappings; unsupported
maps are rejected before submission.

Live Anthropic result: HTTP 400 `invalid_request_error`, account credit balance too low.
No credit purchase or repeated live test loop was attempted. Add credits and rerun
`LIVE_API_TESTS=1 uv run pytest -m slow` to complete this acceptance criterion.

Infrastructure: all 13 services passed healthchecks with `make up`. `alembic upgrade head`
and `alembic check` passed. Local Langfuse observation API returned the successful Mayura
span with 6 characters, INR 0.012 and USD 0.00012698412698412698, plus the failed Claude
request's metadata-only error span. Vendor-returned content and error text are not traced.

Baseline evals: no Makefile/targets existed. After scaffold: UC1/UC2/UC3 each has 8 fixtures,
evidence-check accuracy 1.0, delimiter integrity 1.0, redaction accuracy 1.0. B6 application
metrics remain explicitly unmeasured. Dependency audit: no known vulnerabilities.
