# Blockers

Items a build prompt could not complete in-session. One entry per item; remove the entry in the
PR that resolves it.

| date | prompt | what | why | what unblocks it | owner |
|---|---|---|---|---|---|
| 2026-09-15 | program/P1-golden-audio | golden-audio regeneration tool not written | the 135 WAVs were recovered from the developer machine and committed, so the tool was not needed to unblock evals | run `program/P1-golden-audio`, now scoped to just `synthesize_audio.py` | |
