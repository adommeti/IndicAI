# P0-spike — Vendor contract spike (run before P1)

<!-- Run: bash scripts/run-prompt.sh uc2 P0-spike -->
Read CLAUDE.md. We need to confirm three vendor facts before building apps/training_localizer. Use the sarvam MCP tools and the Sarvam API docs (fetch https://docs.sarvam.ai as needed; use the context7 MCP if it has Sarvam docs indexed).
1. Dubbing API: determine whether a job accepts a caller-supplied translated script/segments, or only source media. Produce a 30-second sample: take samples/intro_en.mp4 (create a 30s test clip with ffmpeg and a Bulbul English narration if no sample exists), submit a Hindi dub, poll, download, and save the result and the API's returned transcript/timings.
2. Mayura: confirm the parameters for mode=formal, output script control, and numeral style; translate 5 sample segments to hi-IN, te-IN, ta-IN.
3. Bulbul: list available voices per language and pick a default per language for narration; render one 20-second sample each.
Write docs/adr/0003-dubbing-contract.md recording what you found (with request/response snippets), and which production path (D9 a or b) we will use. Do not build the pipeline yet. Include in the report: the ADR and the sample outputs.

## Execution notes
- Needs `SARVAM_API_KEY`, `ffmpeg` (installed by the environment setup script), and the `sarvam` MCP server. Dubbing costs ₹40/min per language: keep the clip at 30 s (≈ ₹20). Print the estimate first.
- Save outputs under `docs/adr/assets/0003/` (small: the returned JSON, timings, and a 30 s MP3/MP4 ≤ 5 MB) and write `docs/adr/0003-dubbing-contract.md` with request/response snippets (keys redacted) and the chosen path (D9 a or b).
- Do not build the pipeline. The report includes the ADR and the sample file list with sizes and hashes.
