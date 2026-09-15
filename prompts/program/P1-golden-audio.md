# P1-golden-audio — Restore and pin the UC1 golden audio

<!-- Run: bash scripts/run-prompt.sh program P1-golden-audio -->
Read CLAUDE.md, platform/eval/README.md and platform/eval/golden/uc1_helpdesk/README.md. The 135 Bulbul WAVs referenced by `platform/eval/golden/uc1_helpdesk/audio_manifest.jsonl` exist only on the original developer machine; every cloud session starts without them, and `make eval-uc1` refuses to run when they are absent. Make the golden audio durable.

1. Write `platform/eval/golden/tools/synthesize_audio.py`: for each manifest row, call Bulbul (via the `sarvam` MCP `sarvam_tools_tts_speak` tool from the session, or the `SarvamTTS.speak` adapter from the script — pick one and say which) with the recorded speaker/model/sample-rate, write `audio/<id>.wav`, and verify the SHA-256 against the manifest. Idempotent: skip files that already match.
2. Run it. If every hash matches, un-ignore `platform/eval/golden/uc1_helpdesk/audio/*.wav` in `.gitignore` (synthetic content; total size must stay under 30 MB — report the size) and commit the WAVs. If any hash differs (vendor output changed), do not edit the existing manifest: write `audio_manifest.<YYYYMMDD>.jsonl` with the new hashes and request ids, document the drift in the golden README, add a `--manifest` option to `run_uc1.py` defaulting to the newest manifest, and commit the WAVs.
3. Add a test that fails when a manifest row has no WAV or a mismatched hash (skipped only when the audio directory is absent AND `LIVE_API_TESTS!=1`).
4. Run `make eval-uc1` end to end on the restored audio (baseline decision, retrieval on with `make stack-core` + `make stack-sparse` + `make ingest-kb`) and paste the WER-per-language and hit@3 tables into the report.
Acceptance: all 135 WAVs present and hash-verified; WAVs committed; `make eval-uc1` runs without manual steps in a fresh clone; WER and hit@3 numbers reported.

## Execution notes
- Cost: 135 short TTS calls ≈ 20K characters ≈ ₹60; `make eval-uc1` STT ≈ ₹40. Print both estimates first.
- Requires `SARVAM_API_KEY`; without it, ship the tool + test and mark the criteria UNMEASURED with a `docs/build/BLOCKERS.md` entry.
- Prefer the adapter path in the script (reproducible outside a session); use the MCP tool only to confirm the speaker catalogue if the manifest's speaker names are rejected.
