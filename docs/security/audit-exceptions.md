# Accepted `pip-audit` findings

Every entry here is a vulnerability the audit reports and CI is told to ignore. An entry is a
claim that someone checked reachability and decided; it is not a way to make CI quiet. Each one
names the advisory, why this repository is not exposed, and what would remove the exception.

Re-read this file whenever a dependency moves. An exception that was true for one version is not
automatically true for the next.

## PYSEC-2026-3740 — `nltk` (file sandbox bypass)

- **Aliases:** CVE-2026-81726, GHSA-8mgp-746c-j5xp
- **Present at:** `nltk` 3.10.3, pulled in transitively by `pipecat-ai` 1.10.0 (uc1/P5).
- **Fixed in:** nothing. The advisory names no fix version, and none was published at the time
  this exception was written.

**What the advisory is.** Several model-artifact APIs treat caller-controlled model paths as
ordinary filenames even when NLTK path security is enforced, so a path outside the intended root
is accepted where the guarded helpers would reject it. The named components are
`TransitionParser.train`, `TransitionParser.parse`, `AveragedPerceptron.save`,
`AveragedPerceptron.load`, `PerceptronTagger.save_to_json` and `save_maxent_params` — all of them
load or save a model artifact from a path the caller supplies.

**Why this repository is not exposed.** The exposure requires calling one of those APIs with a
path an attacker can influence.

- No application or platform module imports `nltk`. Verified:
  `grep -rn "import nltk\|from nltk" apps/ platform/` returns nothing.
- The only consumer is `pipecat`, in exactly one place —
  `pipecat/utils/string.py`, which lazily imports `nltk.tokenize.sent_tokenize` and the
  `punkt_tab` data for sentence segmentation. It touches none of the affected components.
- Nothing in this repository passes a path to NLTK at all, so there is no caller-controlled path
  to escape with.

**What would remove this exception.** An `nltk` release that fixes the advisory (then drop the
ignore and let the lock float forward), or dropping `pipecat-ai`, which is the only reason `nltk`
is in the tree.

**Unrelated concern found while reviewing this, recorded so it is not lost.**
`pipecat/utils/string.py` calls `nltk.download("punkt_tab")` on first use if the data is absent —
a network fetch at runtime, inside a voice turn, into whatever `NLTK_DATA` points at. For a
deployment without egress that is a first-turn failure rather than a slow first turn, and for one
with egress it is an unpinned download on a latency-critical path. The fix is to pre-install
`punkt_tab` into the image and set `NLTK_DATA`. Tracked in `docs/build/BLOCKERS.md`.
