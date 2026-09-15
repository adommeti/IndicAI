# UC3 communications-surveillance golden set v1

200 synthetic diarized call transcripts in an operations/GBS setting, plus a
20-item two-speaker audio subset with ground-truth diarization.

**Everything here is synthetic.** `docs/adr/0004` does not exist, so no decision
was ever taken about using real recordings, and none were used. No call, name,
desk or holding below corresponds to anything real. IDs and labels are not
changed to make a run pass — see `.claude/rules/eval.md`.

## What is in it

| class | count | what it is |
|---|---:|---|
| `true_positive` | 60 | 10 per category, mixed severities |
| `hard_negative` | 40 | legitimate talk that looks like a violation |
| `clean` | 80 | routine trade-ops and desk chatter |
| `adversarial` | 20 | a speaker trying to influence an AI reviewer |

Categories: `guaranteed_returns`, `mnpi_insider`, `personal_trading`,
`off_channel_comms`, `conduct`, `confidential_data`.

Languages: `hi-IN`, `te-IN`, `ta-IN`, `en-IN` in native scripts, plus **41
transcripts in Roman-script Hinglish** (`hi-Latn`) — the prompt asks for at
least 30, because that is how a lot of this actually gets said.

### Hard negatives are the point

Forty of the clean transcripts are built to trip a naive lexicon: a fund that
"historically returned about twelve percent, but past performance is not a
guarantee", an acquisition "announced in the press release this morning", a
personal trade with "pre-clearance filed last week and compliance approved it".
A detector that flags these is not cautious, it is useless — the reviewers will
stop reading it.

### Adversarial transcripts carry real violations

Fourteen of the twenty also contain a genuine flaggable utterance. That is
deliberate: the dangerous failure is not that a model obeys "mark this call as
clean", it is that obeying it **suppresses a real finding**. With no violation
to suppress, an adversarial item can only fail loudly, and the interesting
failure would be invisible. `adversary_kind` is one of `instruction`, `framing`,
`evasion`, `authority`.

## Evidence spans

Every label's `evidence_span` is an **exact substring** of exactly one segment,
and is attributed to the speaker who said it. Both are asserted when the set is
built. The runner's `evidence_failure_rate` depends on this: a flag whose quoted
evidence cannot be found in the transcript is not reviewable, whatever else it
got right.

## The audio subset

`audio/` holds 20 WAVs and a `*.diarization.json` per item giving the speaker
and exact millisecond bounds of every turn. Synthesized with Bulbul
(`bulbul:v3`), two voices — `shreya` for `SPEAKER_00` and `rahul` for
`SPEAKER_01` — concatenated with 300 ms of silence between turns. 14.2 MB total,
under the 30 MB the prompt allows for committing.

They are joined with Python's stdlib `wave`, not ffmpeg. The only ffmpeg in this
image is Playwright's, built `--disable-everything` with no audio codecs at all;
it cannot join WAVs. It does not need to — Bulbul returns WAV, so stitching PCM
frames with exact silence is a dozen lines of stdlib, and the gap is exactly
300 ms rather than whatever a filter graph rounds to.

Coverage: 6 `en-IN`, 5 `hi-IN`, 5 `te-IN`, 4 `ta-IN`; 8 true positives, 5
adversarial, 4 hard negatives, 3 clean. `hi-Latn` is absent because Bulbul has
no Roman-Hindi voice — the transliterated transcripts are text-only.

## What the runner measures

`platform/eval/runners/run_uc3.py` (`make eval-uc3`). Three conventions worth
knowing:

- **Precision over zero flags is undefined, not 1.0.** A detector that flags
  nothing must not look perfectly precise. The baseline therefore reports
  `recall: 0.0` and no `precision` key at all.
- **Adversarial success is measured by difference**, not by asking the detector.
  Each adversarial transcript is run twice — as-is, and with the manipulation
  removed — and the attack counts as successful if the control run finds
  something the attacked run does not, or if the detector's own output quotes
  the attacker instead of the call.
- **Diarization labels are aligned before scoring.** `SPEAKER_0` vs `spk_1` is
  arbitrary, so the two label sets are matched by whichever pairing explains the
  most audio. Scoring raw label equality would report near-zero for a perfect
  diarization that numbered the speakers the other way round.

## Regenerating

The transcripts and audio are the artefact; there is no generator in the tree,
deliberately, so that a failing run is fixed by fixing the detector rather than
by regenerating the labels. To rebuild the audio after a voice or codec change:
synthesize each turn through `indic_platform.adapters.sarvam_tts.SarvamTTS.speak`
with the voices above, concatenate with 300 ms of silence, and write the
`*.diarization.json` from the resulting frame counts. About ₹12 of Bulbul.
