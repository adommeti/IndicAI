# Voice demo — speak into a browser, hear a Hindi reply

**Read this first.** This is a set of instructions for a laptop with a working Docker
daemon. It is **not** a report of a run. Nothing below was executed: the build session for
uc1/P5 is a cloud session with no Docker daemon, so LiveKit never started, no browser ever
joined a room, and no voice turn was ever spoken. Every number in this file is either a
budget copied from the PRD, a rate copied from `platform/config/pricing.yaml`, or arithmetic
over those rates — all of it labelled **expected**. There are no observed latencies and no
sample transcripts from a real turn in this document, because there was no real turn.

The acceptance criterion this document serves ("a demo script lets me speak into the browser
and hear a Hindi reply") is therefore **UNMEASURED**, and is recorded as such in
`docs/build/BLOCKERS.md`. Running the steps below on a laptop is what measures it.

One thing will not work as shipped: **there is no recorded consent-notice audio in this
repository** (see step 5.1 and the README's voice section). Read that before you demo this
to anyone.

---

## 0. What you need

| Requirement | Why | Check |
|---|---|---|
| Docker daemon + `docker compose` | LiveKit, Postgres, Redis, Qdrant, MinIO, TEI | `docker ps` |
| RAM headroom | `make stack-core` alone is about 5 GB on the build's 16 GB cloud budget (`docs/build/RUNBOOK.md` section 1); LiveKit and LiveKit SIP sit on top of that. Neither figure was measured on a laptop | |
| `uv`, Python 3.12 | the agent side | `uv run python -V` |
| A microphone and Chrome or Firefox | you are the employee in this demo | |
| `SARVAM_API_KEY` | Saaras STT and Bulbul TTS — **this costs money**, see step 2 | |
| `ANTHROPIC_API_KEY` | `helpdesk_agent.graph:decide` produces the reply | |

Without `ANTHROPIC_API_KEY` there is no reply to speak, and the failure you see will be a
Claude failure, not the STT-failure path in step 6. Without `SARVAM_API_KEY` nothing is
transcribed at all.

---

## 1. Bring the stack up

```bash
make bootstrap      # once per checkout: writes .env.stack (mode 0600, never committed)
make stack-voice    # postgres redis qdrant minio tei + livekit + livekit-sip
make migrate        # creates the turns table this demo writes to
make stack-status   # health table
```

`make stack-voice` runs `scripts/stack.sh voice`, which is `docker compose up -d --build
--wait` over the core set **plus** `livekit` and `livekit-sip`. What that gives you, read
from `docker-compose.yml` and `infra/livekit.yaml`:

| Service | Image | Published on (loopback only) | Config |
|---|---|---|---|
| `livekit` | `livekit/livekit-server:v1.9.0` | `127.0.0.1:7880` (HTTP/WebSocket), `127.0.0.1:7881` (TCP RTC), `127.0.0.1:50100-50120/udp` (RTC media) | `infra/livekit.yaml`; Prometheus on 6789 inside the network |
| `livekit-sip` | `livekit/sip:latest` | `127.0.0.1:5060` tcp+udp, `127.0.0.1:50200-50220/udp` | `infra/sip.yaml` |

Notes that will save you time:

- Every port is bound to `127.0.0.1`. The demo is local-only; nothing is reachable from
  another machine, which is the right default for a service that carries employee audio.
- LiveKit uses the stack's `redis` service (`redis:6379` inside the compose network) and
  starts only after Redis is healthy.
- `livekit-sip` is in the `voice` set but the browser demo does not use it. Because
  `scripts/stack.sh` passes `--wait`, an unhealthy `livekit-sip` makes `make stack-voice`
  exit non-zero even when LiveKit itself is fine — check `make stack-status` before assuming
  the demo is blocked. `livekit/sip:latest` is an unpinned tag, so what you get depends on
  when you pull.
- First run of `tei` downloads the bge-m3 model; its healthcheck allows a 15-minute start
  period and `--wait-timeout` is 1200 s. The voice demo does not need TEI directly, but the
  retrieval step inside `decide` does.

Tear down when you are finished (this also stops the Sarvam meter, see step 2):

```bash
bash scripts/stack.sh down
```

---

## 2. Environment, tokens and what this costs

### Variables

`make bootstrap` generates `.env.stack` (see `infra/bootstrap.py`) with the local-only stack
credentials, including:

```
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=<STACK_PASSWORD, 32 hex chars, generated>
```

`.env.stack` is private, mode 0600, git-ignored, and is never read or edited by the build
agent. `docker-compose.yml` passes the same pair into LiveKit as `LIVEKIT_KEYS: 'devkey:
${STACK_PASSWORD}'`, so the key the server trusts and the key you sign tokens with are the
same by construction — do not hand-edit one of them.

Vendor keys live in `.env`, not in `.env.stack` and never in the repo:

```
SARVAM_API_KEY=...        # Saaras (STT) and Bulbul (TTS)
ANTHROPIC_API_KEY=...     # graph.decide
LIVEKIT_URL=ws://localhost:7880
```

`.env.example` lists all three with comments; it holds no values.

### Where a LiveKit token comes from

LiveKit has no login. A participant joins with a JWT signed by the API secret, carrying the
room name, an identity and grants. Mint one for the browser (this snippet was run offline
during the build and produces a token; it does not need Docker):

```bash
set -a; . ./.env.stack; set +a
uv run python - <<'PY'
import datetime, os
from livekit import api

token = (
    api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
    .with_identity("employee-demo")
    .with_name("Demo employee")
    .with_grants(
        api.VideoGrants(
            room_join=True,
            room="helpdesk-demo",
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )
    )
    .with_ttl(datetime.timedelta(hours=1))
)
print(token.to_jwt())
PY
```

`livekit-api` 1.2.1 is already a dependency (commit `bc6ee44`); do not `uv add` it again.
Give the token a short TTL and treat it as a credential: anyone holding it can join that
room and hear the call. The agent side needs its own token under its own identity — that is
`VoiceSettings.identity`, and whatever entry point `voice_pipeline.py` exposes mints or
receives it; read that module rather than assuming.

### What a voice turn costs

Both Sarvam legs bill. These are the planning rates in `platform/config/pricing.yaml`
(₹94.5/$ indicative). They are the rates this repo prices spans with — they are not an
invoice, and Sarvam's own metering is the authority on what you are charged.

| Leg | Rate | Billed on |
|---|---|---|
| Saaras `saaras:v3` (STT) | ₹30/hour = **₹0.0083 per second** = ₹0.50 per minute | every PCM16 byte the adapter sends: `platform/adapters/sarvam_stt.py` meters `len(chunk)/32000` seconds at 16 kHz mono |
| Bulbul `bulbul:v3` (TTS) | ₹3 per 1,000 characters = **₹0.003 per character** | the redacted reply text sent to the vendor |
| Claude `claude-sonnet-5` (`decide`) | $2/MTok input, $10/MTok output, cache read 0.1× input | the cached system prompt, the last-eight-turn history, the retrieved chunks and the utterance |

Expected arithmetic for one turn — **arithmetic over the rates above, not a measurement**:

- 10 seconds of speech into Saaras: 10 × ₹0.0083 ≈ **₹0.08**
- a 300-character Hindi reply out of Bulbul: 300 × ₹0.003 = **₹0.90**
- a `decide` call of roughly 4,000 input and 200 output tokens: ≈ $0.010 ≈ **₹0.95**
  (the token counts are illustrative; `adapter_calls` records the real ones)

So **budget about ₹2 per voice turn**, roughly half Sarvam and half Claude, with the Sarvam
half dominated by TTS characters rather than STT seconds. A six-turn demo is therefore about
₹12. The greeting adds Bulbul characters every session if it is synthesized rather than
played from a recorded file — one more reason the notice should be a recorded asset.

The one cost that can surprise you: **Saaras bills for whatever audio the pipeline streams
to it, including silence.** If `voice_pipeline.py` gates the STT stream on Silero VAD, you
pay for speech only; if it streams the open microphone continuously, you pay ₹0.50 for every
minute the room is open, whether or not anyone is talking. Read the pipeline before leaving
a demo room idle, and close the room when you are done rather than leaving the tab open.

---

## 3. Start the agent in the room

`apps/helpdesk_agent/voice_pipeline.py` is the agent side. Its pinned contract is:

```python
VoiceSettings(room=..., livekit_url=..., identity=..., language="hi-IN", greeting_path=None)
await run_session(settings, decide=..., session_id=..., employee_id=...)
```

Start it the way that module documents — if it ships a `__main__`, use that; otherwise call
`run_session` from a short script. Do not guess the entry point from this file; `make
voice-test` and the module's own docstring are the authority, and they were written by a
different change than this document.

Use room `helpdesk-demo` (the same room the token in step 2 grants) and `language="hi-IN"`.

Keep the agent's stdout visible in a terminal. It is where you will see the pipeline's own
stage timings and any STT error, and it is the fastest way to tell a vendor failure apart
from a browser microphone-permission failure.

---

## 4. Join from a browser

There is no browser client in this repository — `apps/helpdesk_agent/ui/` contains only a
`.gitkeep`. Use an external LiveKit client. Two options; neither was exercised during the
build, so treat the specifics as "expected" and the LiveKit documentation as authoritative.

**Option A — LiveKit's Meet sample.** Clone `livekit-examples/meet`, configure it per its own
README with `ws://localhost:7880` and the token from step 2, and run it on `http://localhost`.
This is the maintained official client, so microphone capture and audio playback are the
paths most likely to just work. It will not necessarily render this pipeline's custom
data-channel payloads; use the browser console or the agent's stdout for those.

**Option B — a ten-line page you serve yourself**, which shows the data messages. Save this
outside the repository (it is not a repository file), serve it over `http://localhost` so the
browser treats it as a secure context and grants microphone access, and open it:

```bash
mkdir -p /tmp/lkdemo && cd /tmp/lkdemo   # then save the HTML below as index.html
python3 -m http.server 8090              # open http://localhost:8090/
```

```html
<!doctype html>
<meta charset="utf-8" />
<input id="tok" size="60" placeholder="paste the token from step 2" />
<button onclick="go()">join</button>
<pre id="log"></pre>
<script src="https://cdn.jsdelivr.net/npm/livekit-client/dist/livekit-client.umd.js"></script>
<script>
  const log = (m) => (document.getElementById('log').textContent += m + '\n');
  async function go() {
    const room = new LivekitClient.Room();
    room.on(LivekitClient.RoomEvent.DataReceived, (payload) =>
      log('data: ' + new TextDecoder().decode(payload)));
    room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
      if (track.kind === 'audio') document.body.appendChild(track.attach());
      log('subscribed: ' + track.kind);
    });
    await room.connect('ws://localhost:7880', document.getElementById('tok').value);
    await room.localParticipant.setMicrophoneEnabled(true);
    log('joined; mic live');
  }
</script>
```

Written against the LiveKit JS SDK v2 API and **not executed here**. If a name has moved, the
browser console will name it; check the SDK documentation rather than guessing. A hosted
playground page served over `https://` may refuse a `ws://localhost` connection as mixed
content depending on the browser — that is why both options above stay on `http://localhost`.

### What to say

Speak Hindi, after the greeting has finished (step 5.1). A known-good utterance is golden
item `uc1-hi-001`, whose expected action is `answer` citing the VPN article:

> घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।
>
> *(Ghar se VPN connect karne ka tareeka bata dijiye — "tell me how to connect to the VPN
> from home".)*

`uc1-hi-002` — "मेरा ऑफिस वाला पासवर्ड एक्सपायर हो गया है, लॉगिन नहीं हो रहा।" — is a
`file_ticket` item if you want to exercise that branch. Both, with 133 more, are in
`platform/eval/golden/uc1_helpdesk/manifest.jsonl`, and the synthesized audio for them is in
`audio/` next to it if you would rather play a WAV than speak.

Then stop talking and wait. End-of-speech is detected by VAD; talking over the reply is a
different test.

---

## 5. What to look for

Five things, in the order they should happen. Each "expected" below is a requirement from the
prompt, the pinned contract or PRD C5 — **not something observed**.

### 5.1 The consent notice plays before any audio is consumed

**Expected:** within a second of the agent joining, and *before* the pipeline starts feeding
your microphone into Saaras, a short recorded notice plays saying the call is recorded and
transcribed (PRD C8). You should be able to hang up during it and have nothing captured.
That ordering is the whole point: a notice that plays after the first utterance has already
been streamed to a vendor is not a notice.

**As shipped, this will not happen.** There is no consent-notice audio file in this
repository — the only committed audio is the 155 golden WAVs under
`platform/eval/golden/**` and three Bulbul samples under `docs/adr/assets/0003/`, none of
which is a notice. `VoiceSettings.greeting_path` defaults to `None`, so a session started
without a file plays nothing. Recorded in `docs/build/BLOCKERS.md` (uc1/P5).

To demo the *mechanism* before the real asset exists, synthesize a stand-in through the
Sarvam TTS adapter and pass its path as `greeting_path` — and label it a stand-in, because a
production notice is Legal/HR text that a human records and signs off, not a line an engineer
writes in a demo script.

### 5.2 Partial transcripts arrive as data messages

**Expected:** while you are still speaking, the room receives data messages carrying partial
transcripts, and one final message flagged as final. In Option B they appear as `data:` lines
in the log; in Option A, in the console or the agent's stdout.

The exact JSON keys are whatever `voice_pipeline.py` publishes — read that module, do not
assume a shape from this document.

### 5.3 The reply is spoken, in Hindi

**Expected:** audio starts before `decide` has finished producing the whole reply, because
the reply is streamed to Bulbul sentence by sentence (PRD C5, step 15-19 of the C4 sequence).
The voice is the Bulbul speaker configured for `hi-IN`. The reply should be in Hindi, in
Devanagari, because you asked in Hindi — an English reply to a Hindi question is a real
failure, not a cosmetic one, and it is exactly what the reply-language gate in `make
eval-uc1` measures.

### 5.4 Per-stage latencies land in `turns.latency_ms`

`turns.latency_ms` is a JSONB column (`platform/db/models.py`). After a turn:

```bash
set -a; . ./.env.stack; set +a
docker compose --env-file .env.stack exec -T postgres \
  psql -U platform -d platform -c \
  "select id, created_at, language, latency_ms from turns order by created_at desc limit 3;"
```

**Expected:** the newest row is your turn, and `latency_ms` carries the per-stage keys the
contract names — `vad_ms`, `stt_ms`, `decide_ms`, `tts_ms`, `time_to_first_audio_ms` — merged
with the stage timings `graph.decide` already writes. Confirm the key names against
`voice_pipeline.py`; the contract is a contract, not a measurement.

For scale, PRD C5's p50 budget: VAD 250 ms, Saaras final 300 ms, retrieval 150 ms, Claude
first sentence 800 ms, Bulbul first audio 250 ms, transport 100 ms, ≈ 1.85 s to first audio.
**One browser turn is an anecdote, not the gate.** The gate is p50 ≤ 2.0 s and p95 ≤ 3.5 s
over 30 golden items, and the only thing that measures it is `make voice-test`.

### 5.5 Cost shows up where it should

Each adapter call writes an `adapter_calls` row with latency, units and INR/USD cost, and a
Langfuse span carrying the same metadata. **Expected:** two Sarvam rows (Saaras seconds,
Bulbul characters) and at least one Claude row per turn. Spans carry metadata only — never
audio, never transcript text. If you see a turn with no Saaras row, the STT leg did not run
and step 6 is what you are looking at.

---

## 6. What failure looks like

The designed failure is **STT dies, the session switches to chat**. PRD C9: a Saaras stream
error or 429 is retried once, then the session moves to chat mode with a visible notice.

Force it without touching the code by making Saaras unreachable for one session — start the
agent with a deliberately wrong `SARVAM_API_KEY`, or block `api.sarvam.ai` at the firewall
after the room is up.

**Expected:** the room receives a data message telling the client to switch to chat mode, and
the pipeline stops consuming microphone audio. Two things must both be true: you are told,
and the pipeline stops listening. Silence in a room that is still streaming your microphone
to a dead socket is the failure this path exists to prevent — and, given that Saaras bills on
bytes sent, it would also keep costing money.

Other failures and how they differ:

| What you see | Likely cause |
|---|---|
| No audio at all, no data messages, agent stdout quiet | the browser never joined: token expired, wrong room name, or `ws://localhost:7880` not up (`make stack-status`) |
| Joined, mic live, no partials | the agent is not in the room, or is in a different room |
| Partials fine, no reply | `ANTHROPIC_API_KEY` missing or `decide` failing — check agent stdout; this is not the chat-mode path |
| Reply text appears but no audio | Bulbul leg — check the `adapter_calls` row for `bulbul:v3` |
| Chat-mode data message | the intended STT-failure path, above |

---

## 7. When you have run this

The criterion is measured by a person, not by CI. If it worked, say so with what you
observed — including the `latency_ms` row — and remove the uc1/P5 browser-demo row from
`docs/build/BLOCKERS.md` in the same change. If it did not, record what failed. Do not mark
it passed from this document: this document has never been run.
