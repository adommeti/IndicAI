---
name: live-vendor
description: Safely make live Sarvam and Anthropic calls during a build — MCP tools for verification and golden-audio synthesis, LIVE_API_TESTS smoke tests, cost estimation, and what to do when a key or credit is missing. Use before any live vendor call, TTS synthesis, dubbing spike, or live eval.
allowed-tools: Bash(uv *), Bash(make *), Bash(ffmpeg *), Bash(ffprobe *), Bash(sha256sum *), Read, Write
---
# Live vendor calls

## Preconditions
- `SARVAM_API_KEY` / `ANTHROPIC_API_KEY` are environment variables in the cloud environment (or
  `.env` locally, loaded by `indic_platform.config.settings`). The SessionStart banner says which
  are present. Never print, echo, or write a key anywhere.
- Before a batch of calls, print an estimate using `platform/config/pricing.yaml` rates:
  STT ₹30/h (₹45/h diarized), TTS ₹3/1K chars, Mayura ₹2/1K chars, dubbing ₹40/min,
  Sonnet 5 $2/$10 per MTok, Haiku 4.5 $1/$5. Keep single-prompt vendor spend under ~₹500 / $5
  unless the prompt explicitly budgets more (UC2 dubbing spike ≈ ₹40/min × minutes × languages).

## Which path to use
| Need | Use |
|---|---|
| Confirm an endpoint's request/response shape | `sarvam` MCP tool once, then write the adapter test from the real shape; note it in `docs/adapter-verification.md` |
| Synthesize golden audio | `sarvam` MCP `sarvam_tools_tts_speak` (Bulbul v3, 16 kHz WAV); speakers per language as in `golden/uc1_helpdesk/README.md` (hi `priya`, te `kavya`, ta `vijay`); record sha256, duration, speaker, model, request id in the audio manifest |
| Two-speaker synthetic call audio (UC3) | two TTS voices → `ffmpeg` concat with 300 ms silence gaps → WAV + ground-truth diarization JSON |
| Adapter smoke | `LIVE_API_TESTS=1 uv run pytest -m slow -k <vendor or capability>`; one call per capability |
| Full live eval | `make eval-uc1` (STT for 135 items), later `make eval-uc2` judge calls, `make eval-uc3` diarization subset |

## When a key is missing or a call fails with billing/credit errors
Do not retry in a loop. Mark the dependent criteria UNMEASURED, keep the mocked tests green,
add a `docs/build/BLOCKERS.md` entry naming the vendor, the error class (no key / 401 / credit
balance / 429 sustained), and the exact command to re-run once fixed. Ship the rest.

## Determinism and provenance
Every synthesized artifact is recorded with hash + vendor request id in a manifest next to it.
Large media stays out of git (`platform/eval/golden/**/audio/*.wav` is ignored); manifests are
committed. Re-synthesis must reproduce the manifest hash or produce a new dated manifest.
