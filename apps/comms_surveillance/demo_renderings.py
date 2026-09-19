"""English renderings for the demo queue's evidence spans.

`demo_seed` makes no vendor call, by design: it runs offline, in CI, and on a
laptop with no keys. But two thirds of the calls in the golden set are in Hindi,
Telugu, Tamil or Hinglish, and a reviewer console showing a Devanagari evidence
span with an empty box beside it labelled "English rendering" is demonstrating
the wrong thing.

So the renderings are produced **once**, here, by the same function the pipeline
uses (`detector.render_english`, Haiku), and checked in to `demo/renderings.json`
with the model and the date that produced each one. The seeder reads that file and
nothing else. What the demo shows is therefore a real translation by a named
model on a recorded date -- not a gloss this repository wrote by hand and not an
empty field.

Two things this deliberately does NOT do:

- it does not make the *finding* look model-produced. `analysis_runs.model` stays
  `demo-seed` and the flag's `reasoning` still says where the flag came from. A
  translated span is a translated span; it is not a judgement.
- it does not run at seed time. A `make seed-uc3` that phoned a vendor would
  spend money on every demo, fail on a machine with no key, and produce a
  different queue each time.

Regenerate with:

    ENV=dev uv run python -m comms_surveillance.demo_renderings --write

It prints the estimated cost first (`.claude/rules/eval.md`) and needs an
Anthropic key. Spans that already have a rendering are not re-translated, so a
re-run after the golden set grows costs only the new ones.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

from indic_platform.eval.runners.run_uc3 import Transcript, load_transcripts

from comms_surveillance import detector

STORE = Path(__file__).parent / "demo" / "renderings.json"

#: Prefixes every key. Without it the keys are bare 16-character hex strings,
#: which the repository's secret scan reports as high-entropy strings -- a whole
#: file of false positives, and the only ways to silence them would be to
#: allow-list the file (so a real secret in it would never be seen again) or to
#: accept a permanently red gate.
KEY_PREFIX = "span:"

#: Spans in these languages are already English; translating them would spend
#: money to get the same string back.
NATIVE_ENGLISH = frozenset({"en-IN"})


def span_key(span: str) -> str:
    """Keyed by the span's own digest, not by the golden id that carries it.

    The same sentence appears in more than one transcript, and a transcript's id
    can change; the text cannot. This also means a rendering stays valid when the
    seeder's selection changes.
    """
    return KEY_PREFIX + hashlib.sha256(span.strip().encode()).hexdigest()[:16]


@cache
def _payload() -> dict[str, Any]:
    """The file, parsed once. Read on every call it would be read 48 times in
    one `build_dataset`, for a file that cannot change while the process runs."""
    if not STORE.exists():
        return {}
    return dict(json.loads(STORE.read_text()))


def load() -> dict[str, dict[str, str]]:
    """The checked-in renderings, keyed by span digest.

    Each entry is `{"english", "model", "generated_at"}`. The model and date are
    stored **per span**, not once for the file: a later run translates only what
    is missing, so a file-level stamp would relabel every older translation with
    whatever model happened to run last. Provenance that moves when you add a row
    is not provenance.

    Empty is a supported state: `demo_seed` then leaves `english_rendering` blank,
    which is exactly what `detector.render_english` returns when its call fails,
    and which the console renders as "no English rendering was produced".

    A copy, not the cached dict: `generate` mutates what `load` hands it.
    """
    return {key: dict(entry) for key, entry in _payload().get("spans", {}).items()}


def provenance() -> dict[str, Any]:
    """Who produced the renderings -- the function -- and nothing about the
    findings they sit beside. The model and date are per span; `demo_seed` adds
    the ones a given call actually used."""
    return {k: v for k, v in _payload().items() if k != "spans"}


def spans_needing_a_rendering(
    transcripts: list[Transcript] | None = None,
    known: dict[str, dict[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Every (span, language) in the corpus that is not English and not yet done.

    Covers the whole corpus rather than one `--calls` slice, so a later seed with
    a different count or a different selection is still fully rendered.
    """
    have = load() if known is None else known
    items = transcripts if transcripts is not None else load_transcripts()
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for item in items:
        if item.language_mix in NATIVE_ENGLISH:
            continue
        for segment in item.segments:
            key = span_key(segment.text)
            if key in have or key in seen:
                continue
            seen.add(key)
            out.append((segment.text, item.language_mix))
    return out


def estimate(count: int) -> str:
    """What this will spend, printed before it spends it (`.claude/rules/eval.md`)."""
    import yaml

    pricing = yaml.safe_load(
        (Path(__file__).parents[2] / "platform/config/pricing.yaml").read_text()
    )
    model = pricing["models"][detector.TRIAGE_MODEL]
    fx = float(pricing["fx_inr_per_usd"])
    # A short system prompt plus one sentence in, one sentence out.
    usd = count * (200 * model["input_tokens"] + 80 * model["output_tokens"])
    return (
        f"{count} span(s) to translate on {detector.TRIAGE_MODEL} "
        f"-- estimated ${usd:.3f} / Rs {usd * fx:.1f}"
    )


async def generate(limit: int | None = None, client: Any = None) -> dict[str, Any]:
    """Translate what is missing and merge it into the store.

    `render_english` swallows its own failures and returns "", which is right for
    the pipeline -- an empty rendering beats a wrong one -- and wrong here: a
    generator that silently writes nothing leaves you believing the file is
    complete. So an empty result is counted and reported rather than stored.
    """
    have = load()
    todo = spans_needing_a_rendering(known=have)[: limit if limit is not None else None]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    failed: list[str] = []
    for span, _language in todo:
        rendered = await detector.render_english(
            detector.AnalysisFlag(category="conduct", severity="low", evidence_span=span),
            client=client,
        )
        text = rendered.strip()
        # A result identical to its input is not a translation -- it is the model
        # handing the span back, which happens on Roman-script Hinglish it reads
        # as already English. Stored, it would sit under a heading promising an
        # English rendering while saying nothing the reviewer could not already
        # read. Counted as a failure, the console says so instead.
        if not text or text == span.strip():
            failed.append(span)
            continue
        have[span_key(span)] = {
            "english": text,
            "model": detector.TRIAGE_MODEL,
            "generated_at": today,
        }
    return {"spans": have, "translated": len(todo) - len(failed), "failed": len(failed)}


def write(spans: dict[str, dict[str, str]]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    _payload.cache_clear()
    STORE.write_text(
        json.dumps(
            {
                "note": (
                    "English renderings of the uc3 golden set's transcript turns, for the "
                    "demo queue. Produced by comms_surveillance.detector.render_english. "
                    "These are translations, not findings: the analysis rows that carry "
                    "them are still recorded with model 'demo-seed'."
                ),
                "renderer": "comms_surveillance.detector.render_english",
                "spans": dict(sorted(spans.items())),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="make the calls and save the file")
    parser.add_argument("--limit", type=int, default=None, help="translate at most this many")
    args = parser.parse_args(argv)

    todo = spans_needing_a_rendering()
    capped = todo[: args.limit] if args.limit is not None else todo
    print(estimate(len(capped)))
    if not args.write:
        print(f"{len(todo)} span(s) missing a rendering; pass --write to make the calls")
        return 0
    result = await generate(limit=args.limit)
    write(result["spans"])
    print(
        json.dumps(
            {"translated": result["translated"], "failed": result["failed"], "file": str(STORE)},
            indent=2,
        )
    )
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
