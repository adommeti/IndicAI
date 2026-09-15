---
name: eval-gates
description: How to run, read and act on the offline and live evaluation harness (make eval-uc1/uc2/uc3), the B6 go/no-go gates, and what "unmeasured" means. Use when a prompt's acceptance mentions eval numbers, hit@3, WER, precision/recall, adversarial rate, fidelity, or when diagnosing failing golden items.
allowed-tools: Bash(make *), Bash(uv *), Read, Grep, Glob
---
# Evaluation harness

## Where things are
- Runners: `platform/eval/runners/run.py` (offline scaffold checks, any app),
  `run_uc1.py` (live: Saaras WER on 135 golden WAVs + retrieval + decision), later `run_uc2.py`,
  `run_uc3.py`. Reports land in `docs/eval/<uc>.{json,md}` (gitignored).
- Golden sets: `platform/eval/golden/uc1_helpdesk/` (150 items, 20 adversarial; audio WAVs are
  not in git — they are synthesized with the `sarvam` MCP `sarvam_tools_tts_speak` tool and must
  match `audio_manifest.jsonl` SHA-256s), `uc2_training/`, `uc3_surveillance/` (built by their P1).
- Gates (PRD B6): UC1 WER ≤ 15% hi / ≤ 20% te,ta; action accuracy ≥ 0.85; hit@3 ≥ 0.80;
  groundedness ≥ 0.95 (sampled 50); first audio p50 ≤ 2.0s, p95 ≤ 3.5s. UC2 terminology 100%;
  fidelity mean ≥ 4.0; timing-fit ≥ 90%. UC3 precision ≥ 0.80; recall ≥ 0.85; adversarial 0%;
  diarization ≥ 0.90.

## Running
- Offline, no keys, always in CI and in the Stop gate:
  `uv run python -m indic_platform.eval.runners.run --app uc1|uc2|uc3`
- UC1 full (live Saaras + local retrieval): start `make stack-core` (and `make stack-sparse` for
  hybrid), `make ingest-kb`, then `make eval-uc1`. Needs `SARVAM_API_KEY` and the WAVs. Cost is
  ~135 short STT calls (well under ₹50); print the estimate before running.
- Regenerate missing golden audio: iterate `audio_manifest.jsonl`, call the MCP TTS tool with the
  recorded speaker/model, save to `golden/uc1_helpdesk/audio/<id>.wav`, and verify the SHA-256.
  If a hash differs (vendor output changed), do not edit the manifest: write
  `audio_manifest.<date>.jsonl` and explain in the README, then point the runner at it.

## Reading a failure
1. Open `docs/eval/<uc>.json` and list failing items with their expected vs observed values.
2. Classify: retrieval miss (chunking/embedding/filters), decision error (prompt/guard/schema),
   language mismatch (langid vs script), STT (WER driven), or label suspicion.
3. Fix the system, never the label. If a label is wrong, record it in the report and propose a
   versioned manifest change in `docs/build/BLOCKERS.md` for the owner to approve.
4. Re-run only the affected stage where the runner allows (`--decide`, `--retrieval` flags) and
   then the full target before claiming the gate.
5. Paste the metric table verbatim into `.claude/run/report.md`.

## Unmeasured
A metric is UNMEASURED when its inputs do not exist yet (no agent, no audio, no stack, no key).
The runner must print it as such; the report must say what would make it measurable. A prompt
whose acceptance requires a measured gate is `partial` until it is measured.
