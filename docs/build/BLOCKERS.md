# Blockers

Items a build prompt could not complete in-session. One entry per item; remove the entry in the
PR that resolves it.

| date | prompt | what | why | what unblocks it | owner |
|---|---|---|---|---|---|
| 2026-09-15 | program/P1-golden-audio | golden-audio regeneration tool not written | the 135 WAVs were recovered from the developer machine and committed, so the tool was not needed to unblock evals | run `program/P1-golden-audio`, now scoped to just `synthesize_audio.py` | |
| 2026-09-15 | uc1/P3-eval | no B6 gate for UC1 P3 could be measured; nothing was run and no vendor spend was incurred | Two preconditions named in the prompt's step 1 are absent from the session. (1) `ANTHROPIC_API_KEY` is unset, so `helpdesk_agent.graph:decide` cannot run — that blocks action accuracy, reply-language match, adversarial compliance, and the grounding-verifier retry/fallback/cost figures, i.e. everything `make eval-uc1` (which is `--chat-only`, Claude-only, no Saaras) produces. (2) The Docker daemon is unavailable, so `make stack-core`/`make stack-sparse`/`make ingest-kb` cannot run and hit@3 has no retriever. WER alone was reachable (`SARVAM_API_KEY` is set, 135 WAVs present and hash-verified) but step 1 says to stop rather than report a partial number as a gate, so the ~₹40 of Saaras calls was deliberately not spent | set `ANTHROPIC_API_KEY` in the cloud environment (RUNBOOK §1) and run in a session with a working Docker daemon; then re-run `/run-prompt uc1 P3-eval`. Preconditions already satisfied: 135 golden WAVs present, `platform/tests/test_golden_audio.py` 136 passed | |
