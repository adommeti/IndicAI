# ADR 0003 — Dubbing contract: the API takes a caller-supplied script, so UC2 uses path (a)

- **Status:** Accepted (contract settled); one step unverified — see Evidence
- **Date:** 2026-09-15 · **Owner:** `uc2/P0-spike`
- **Relates to:** ADR 0001 (translation is the product in UC2), ADR 0002, PRD D9

## Context

`docs/prd-v2.md` D9 left one question open: does the Sarvam Dubbing API accept a **caller-supplied,
pre-approved translated script**, or does it always produce its own translation? The two designs
differ materially. Path (a) submits the approved script, so UC2's post-edit → QA → reviewer chain
stays authoritative. Path (b) accepts the machine translation and corrects it afterwards, costing an
extra review cycle per module-language, and D9 says to budget for (b) until the spike settles it.

The public documentation describes only "create a job, upload media, start, poll, export" and does
not mention supplying a script, which points at (b). The SDK tells a different story.

## Decision

**Path (a).** Every `dubbing.create` response carries an `srt_upload_url` alongside the media
`upload_url`, and both point into the job's **`inputs/source/`** directory:

```
.../jobs_store/{job_id}/inputs/source/{job_id}.mp4?<sas>     # media
.../jobs_store/{job_id}/inputs/source/{job_id}.srt?<sas>     # caller-supplied script + timings
```

It is an input slot, returned unconditionally — on plain jobs, with `editor_flow=true`, and with
`register=formal` alike. UC2 therefore submits the approved translation as an SRT, and the
reviewer-approved text is what gets spoken. Build `uc2/P2`'s pipeline to emit SRT as its output
artifact, and `uc2/P4` to upload it with the media.

**Corollaries fixed by the same spike:**

- **The dub's own translation register is controllable in-job** — `register` accepts
  `formal | common-indic | classic-colloquial | modern-colloquial | academic | auto`. Useful as a
  fallback, but on path (a) the script is ours, so register only shapes anything we do not supply.
- **No response model carries a transcript or timings.** Across the whole SDK type package, the only
  `transcript` fields belong to speech-to-text models; the dubbing responses expose job status and
  export download URLs only. So even on path (b) the "retrieve the API's transcript" step in D9
  would have to parse the **SRT export**, not a JSON field. Keep `srt` in `export_options` always.
- **`voice_id` is required when `voice_cloning=false`**, and **cloning is the API default**. UC2 must
  pass `voice_cloning=False` with an explicit `voice_id` until an instructor consents (PRD G2 Q6);
  `platform/adapters/sarvam_dub.py` already does this.
- **`editor_flow=true` doubles the price** (₹80/min vs ₹40/min) and only "suppresses auto-export,
  leaving exports for a human to trigger in Creator Studio". On path (a) it buys nothing we need, so
  leave it false.
- **Odia differs by endpoint**: dubbing uses `or-IN`, while translate and TTS use `od-IN`. Anything
  that maps a language across both must translate the code.

## Consequences

- **Positive:** UC2 keeps one source of truth. The glossary, LOCKED renderings and reviewer sign-off
  from `uc2/P1`–`P3` govern the audio, instead of being reconciled against a second machine
  translation after the fact. The extra review cycle D9 told us to budget for is not needed.
- **Positive:** cost stays at the ₹40/min Starter rate; no `editor_flow` premium, no re-dub cycle.
- **Negative:** UC2 now owns segment timing. An SRT must carry cues the dub can hit, so `uc2/P2`
  needs a timing source (the source media's own SRT, or a first pass of the job) and `uc2/P4` must
  measure timing fit against it — which the P4 prompt already requires.
- **Negative, and the reason the status above is qualified:** the SRT *slot* is proven, but a job
  that actually consumes a caller-supplied SRT was never run (see Evidence). If the slot turns out
  to be ignored, or to require an undocumented flag, UC2 falls back to path (b) with the SRT export
  as the transcript source — the same parsing either way, plus the extra review cycle.
- **Negative:** `srt_upload_url` is undocumented. It could change without notice, so `uc2/P4` should
  assert its presence and fail loudly rather than silently skipping the script upload.

## Evidence

- `docs/adr/assets/0003/dubbing-create-response.json` — a create request and its redacted response
  showing `srt_upload_url` under `inputs/source/`, the full `dubbing.create` SDK signature, and the
  three errors observed. The job was created but **never started**, so no minutes were billed.
- SDK `sarvamai==0.1.32`: `dubbing.create(source_language_code, target_language_codes,
  export_options, voice_cloning, voice_id, pace_preset, num_speakers, disable_watermark, register,
  editor_flow, job_name)` — no script, transcript or segment parameter, which is why the *response*
  slot is the answer. `CreateDubbingJobData.srt_upload_url` is the field.
- `platform/adapters/sarvam_dub.py` — the P0 adapter; it does not yet upload an SRT. `uc2/P4` adds
  that. `docs/adapter-verification.md:22` records that the MCP dub tool is a separate
  STT→translate→TTS composition and says nothing about this endpoint.
- **Unverified:** uploading the SRT and starting the job. `PUT` to either upload URL fails with
  `httpx.ProxyError: 403 Forbidden` — `*.blob.core.windows.net` is not on the cloud environment's
  network allowlist (RUNBOOK §1), so no media or script can be uploaded from a session. Recorded in
  `docs/build/BLOCKERS.md`; `uc2/P4` cannot run until the allowlist covers it.
- Mayura, `docs/adr/assets/0003/mayura-samples.json` — 5 compliance segments × hi/te/ta in
  `mode=formal`, plus controls: `numerals_format=native` (१,२५०), `output_script=roman`, and
  `output_script=spoken-form-in-native`, which spells numerals as words ("एक हज़ार दो सौ पचास") and is
  the right pre-TTS form. ₹2.61 for 1,305 chars.
- Bulbul, `docs/adr/assets/0003/bulbul-samples.json` + three `.mp3` files — `bulbul:v3` roster is 38
  voices; the SDK's speaker enum is a v2 ∪ v3 union, and a v2-only voice (`anushka`) is rejected with
  `invalid_request_error`. Narration default per language: **`shreya`** (calm female, news-anchor).
  ₹3.67 for 1,222 chars.
