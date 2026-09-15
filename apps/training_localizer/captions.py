"""WebVTT and SRT for the approved script, and the timings that come back.

Two directions, and both matter:

**Out.** `to_vtt` and `to_srt` render the reviewer-approved segments with the
source timings. The SRT is not only a caption file: ADR 0003 settled that the
dubbing API accepts a caller-supplied script at `inputs/source/{job}.srt`, so
this is what makes the reviewer's text the text that gets spoken (path a).

**In.** `parse_srt_timings` reads the dub's SRT *export*. The same spike found
that no dubbing response model carries a transcript or timings -- only job
status and export URLs -- so the exported SRT is the only place the produced
per-segment durations exist. `timing_fit` compares them to the source.
"""

import re
from dataclasses import dataclass
from typing import Any

# D10: the dub must land within +/-15% of the source segment duration.
TOLERANCE = 0.15


@dataclass(frozen=True)
class Cue:
    seg_id: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def stamp(ms: int, *, sep: str = ".") -> str:
    """`HH:MM:SS.mmm` for WebVTT, `HH:MM:SS,mmm` for SRT."""
    if ms < 0:
        raise ValueError("Timestamps cannot be negative")
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, milliseconds = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{milliseconds:03d}"


def _ordered(cues: list[Cue]) -> list[Cue]:
    ordered = sorted(cues, key=lambda c: (c.start_ms, c.seg_id))
    for cue in ordered:
        if cue.end_ms <= cue.start_ms:
            raise ValueError(f"Cue {cue.seg_id} ends before it starts")
        if not cue.text.strip():
            raise ValueError(f"Cue {cue.seg_id} has no text")
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start_ms < earlier.end_ms:
            raise ValueError(
                f"Cues {earlier.seg_id} and {later.seg_id} overlap; a caption track cannot "
                "show two lines for the same instant"
            )
    return ordered


def to_vtt(cues: list[Cue], *, language: str) -> str:
    """WebVTT for the approved script.

    Cue identifiers are the segment ids, so a caption on screen can be traced
    back to the row a reviewer approved. A blank line ends every block; the
    trailing newline is what strict parsers expect.
    """
    lines = ["WEBVTT", f"Language: {language}", ""]
    for cue in _ordered(cues):
        lines += [
            str(cue.seg_id),
            f"{stamp(cue.start_ms)} --> {stamp(cue.end_ms)}",
            cue.text.strip(),
            "",
        ]
    return "\n".join(lines)


def to_srt(cues: list[Cue]) -> str:
    """SRT for the dubbing job's `inputs/source/{job}.srt` slot (ADR 0003 path a).

    SRT numbers cues from 1 in order and has no header; the segment id goes
    nowhere, so `parse_srt_timings` returns positions and the caller re-keys
    them against the segments it submitted, in the same order.
    """
    blocks = []
    for index, cue in enumerate(_ordered(cues), start=1):
        blocks.append(
            f"{index}\n{stamp(cue.start_ms, sep=',')} --> {stamp(cue.end_ms, sep=',')}\n"
            f"{cue.text.strip()}\n"
        )
    return "\n".join(blocks)


TIMECODE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _ms(hours: str, minutes: str, seconds: str, millis: str) -> int:
    return int(hours) * 3_600_000 + int(minutes) * 60_000 + int(seconds) * 1000 + int(millis)


def parse_srt_timings(srt: str) -> list[tuple[int, int]]:
    """(start_ms, end_ms) per cue, in file order, from an SRT export.

    Deliberately tolerant of the separator: some tools emit `.` where SRT wants
    `,`, and a timing measurement that refuses to read the vendor's file is a
    measurement we do not have.
    """
    return [
        (_ms(*match.groups()[:4]), _ms(*match.groups()[4:])) for match in TIMECODE.finditer(srt)
    ]


def timing_fit(
    source: list[Cue], produced: list[tuple[int, int]], *, tolerance: float = TOLERANCE
) -> dict[str, Any]:
    """How close the dub's per-segment durations landed to the source's.

    This is the only honest place to measure it: the estimate in
    `platform/config/timing.yaml` predicts from character counts, while this
    reads what the vendor actually rendered.

    A produced file with a different number of cues is a hard failure, not a
    partial score: the segments no longer line up, so per-segment comparison is
    meaningless and a number would be worse than none.
    """
    ordered = _ordered(source)
    if len(produced) != len(ordered):
        return {
            "timing_fit_rate": None,
            "segments": len(ordered),
            "produced_segments": len(produced),
            "error": "cue count differs; the dub did not preserve segment boundaries",
        }
    ratios: list[float] = []
    worst: dict[str, Any] | None = None
    fits = 0
    for cue, (start, end) in zip(ordered, produced, strict=True):
        produced_ms = end - start
        ratio = produced_ms / cue.duration_ms
        ratios.append(ratio)
        if abs(ratio - 1.0) <= tolerance:
            fits += 1
        elif worst is None or abs(ratio - 1.0) > abs(worst["ratio"] - 1.0):
            worst = {
                "seg_id": cue.seg_id,
                "ratio": ratio,
                "source_ms": cue.duration_ms,
                "produced_ms": produced_ms,
            }
    return {
        "timing_fit_rate": fits / len(ordered),
        "segments": len(ordered),
        "within_tolerance": fits,
        "tolerance": tolerance,
        "ratio_min": min(ratios),
        "ratio_max": max(ratios),
        "worst_segment": worst,
    }


def validate_vtt(text: str) -> None:
    """Strict parse, so a malformed track fails here rather than in a player."""
    import io

    import webvtt

    parsed = webvtt.from_buffer(io.StringIO(text))
    if not list(parsed):
        raise ValueError("WebVTT parsed but contains no cues")
