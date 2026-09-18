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

## Sarvam `/transliterate` — verified 2026-09-15 (uc3/P2)

Added `SarvamTranslate.transliterate` for the Roman rendering of native-script
transcript segments.

Request shape confirmed against the `sarvam` MCP server's API reference:

| field | value |
|---|---|
| method | `POST /transliterate` |
| body | `input`, `source_language_code`, `target_language_code`, `numerals_format`, `spoken_form` |
| response | `transliterated_text`, `source_language_code` |

Live call made through the MCP tool to confirm the behaviour that motivated the
method — `input="வணக்கம், குரல் சரியாக கேட்கிறதா?"`, `source_language_code="ta-IN"`,
`target_language_code="en-IN"` returned
`"Vanakkam, gaala sariyaaga ketkirathaa?"`. Sarvam gets `Vanakkam` right where
the offline library returns `vaṇaghghaṁ`; it renders `குரல்` as `gaala` rather
than `kural`, so it is better on Tamil, not perfect.

Billed against `mayura:v1` (the per-character text rate). Sarvam publishes no
separate transliteration rate; if one appears it belongs in
`platform/config/pricing.yaml`.

## Anthropic `messages.parse` — response cap, stop reason and latency (uc2/P2-eval)

Verified against the live API over four `make eval-uc2-live` runs on 2026-09-18, driving
`training_localizer.stages` rather than a synthetic probe, so the shapes below are the ones the
pipeline actually produces.

| behaviour | observed |
|---|---|
| a reply that reaches `max_tokens` | `stop_reason == "max_tokens"`, `parsed_output is None`, `usage.output_tokens` exactly equal to the cap. No exception from the SDK — the truncation is only visible in `stop_reason`, which is why `Claude.structured` must inspect it |
| a reply that completes | `stop_reason == "end_turn"`, `parsed_output` populated |
| `max_tokens` on the request | honoured as a ceiling, not a target: the same call returned 2,725 output tokens under an 8192 cap and 5,457 under the same cap for a longer module |
| billing | charged on tokens generated, not on the ceiling, so raising the cap costs nothing until the model uses it |
| `timeout` on the request | reaches the transport as the httpx read timeout; it does not bound the runtime's own `asyncio.timeout`, which is passed separately |

Output-token cost by script, same 18-segment module, one `adapt` call each:

| language | output tokens | wall time |
|---|---|---|
| ta-IN | ~2,900 | 22.5s |
| hi-IN | 2,725 | 25.3s |
| te-IN | 5,457 | 47.8s |

The ratio is the point: Indic scripts tokenize far more heavily than Latin, and Telugu was the
expensive case on every measurement taken. A cap or a clock sized against an English or Hindi
sample is not sized for Telugu. `post_edit` showed the same ordering on a single segment —
hi-IN 345-604, ta-IN 957, te-IN 986 — against a 1024 default that had never been exercised in
anything but Hindi.

Not verified: `backtranslate_qa` and `quiz` beyond a single 691-token `quiz` probe. The account
reached its usage limit before either stage completed live (`docs/build/BLOCKERS.md`).
