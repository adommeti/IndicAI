"""Stage 0 as the eval runner's system under test.

`run_uc3.py` takes `--detect module:function` and calls it with a `Transcript`,
expecting `[Flag]`. This module is the adapter between that contract and the
lexicon matcher, and it is deliberately thin: the matcher stays pure and knows
nothing about the eval's types.

The one judgement here is which hits become flags. A lexicon hit in the Roman
rendering cannot be quoted as evidence -- PRD E5's verifier requires the
evidence span to be an exact substring of the transcript -- so a Roman-only hit
is reported against the native text of the segment it was found in. That keeps
every flag verifiable while still letting a Hinglish term do its job.
"""

from functools import lru_cache
from typing import Any

from comms_surveillance.lexicon import matcher


@lru_cache(maxsize=1)
def lexicon() -> matcher.Lexicon:
    """Loaded once per process. The load is the only I/O; scanning is pure."""
    return matcher.load()


def segments_of(transcript: Any) -> list[matcher.Segment]:
    """A `run_uc3.Transcript` as the matcher's view of it.

    The golden transcripts carry no Roman rendering -- they are what STT would
    have produced -- so the transliteration is derived here, which is also what
    the real pipeline stores alongside the native text (uc3/P2).
    """
    from comms_surveillance.transliterate import offline

    out = []
    for segment in transcript.segments:
        roman, _ = offline(segment.text, transcript.language_mix)
        out.append(matcher.Segment(text=segment.text, text_roman=roman, speaker=segment.speaker))
    return out


def detect(transcript: Any) -> list[Any]:
    """Stage 0 flags for one transcript. Signature required by `run_uc3`."""
    from indic_platform.eval.runners.run_uc3 import Flag

    hits = lexicon().scan(segments_of(transcript))
    flags: list[Any] = []
    seen: set[tuple[str, int, str]] = set()
    for hit in hits:
        native = transcript.segments[hit.segment_index].text
        if hit.field == "text":
            evidence = hit.matched
        else:
            # Found in the transliteration: quote the turn it came from, which
            # is verbatim native text and so survives the evidence verifier.
            evidence = native
        key = (hit.category, hit.segment_index, evidence)
        if key in seen:
            continue
        seen.add(key)
        flags.append(
            Flag(
                category=hit.category,
                evidence_span=evidence,
                speaker=hit.speaker,
                severity=hit.severity,
            )
        )
    return flags
