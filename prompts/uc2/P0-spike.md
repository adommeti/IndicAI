# P0-spike — Vendor contract spike (run before P1)

<!-- Run: runbooks/run-prompt.sh uc2 P0-spike  (or paste into the Codex TUI) -->

Read AGENTS.md. We need to confirm three vendor facts before building apps/training_localizer. Use the sarvam MCP tools and the Sarvam API docs (fetch https://docs.sarvam.ai as needed; use the context7 MCP if it has Sarvam docs indexed).
1. Dubbing API: determine whether a job accepts a caller-supplied translated script/segments, or only source media. Produce a 30-second sample: take samples/intro_en.mp4 (create a 30s test clip with ffmpeg and a Bulbul English narration if no sample exists), submit a Hindi dub, poll, download, and save the result and the API's returned transcript/timings.
2. Mayura: confirm the parameters for mode=formal, output script control, and numeral style; translate 5 sample segments to hi-IN, te-IN, ta-IN.
3. Bulbul: list available voices per language and pick a default per language for narration; render one 20-second sample each.
Write docs/adr/0003-dubbing-contract.md recording what you found (with request/response snippets), and which production path (D9 a or b) we will use. Do not build the pipeline yet. Stop and show me the ADR and the sample outputs.
