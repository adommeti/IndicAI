"""UC1 baseline harness. Live audio is required unless an STT stage is injected."""

import argparse
import asyncio
import hashlib
import importlib
import inspect
import json
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, Self

import jiwer
from indic_platform.eval.report import THRESHOLDS, Report, Thresholds, fails_regression
from pydantic import BaseModel, Field, model_validator

GOLDEN = Path(__file__).parents[1] / "golden/uc1_helpdesk"

# What a mocked decision stage cannot tell you about the agent. A trivial baseline still
# produces an action and a reply, so `action_accuracy` and friends are computable -- and
# computing them would put a number that looks like a quality signal next to numbers that
# are one. `.claude/rules/eval.md`: "every B6 metric is either measured (with the count of
# items it used) or reported as unmeasured with the reason. Never emit a placeholder
# passing score." A failing placeholder is no better; it is still an answer to a question
# the run did not ask. So under `--mocked-decisions` these are dropped, with their reason.
MOCKED_UNMEASURED = {
    "action_accuracy": (
        "mocked decision stage: the actions scored are the trivial baseline's, not the "
        "agent's. Measured by `make eval-uc1` against helpdesk_agent.graph:decide, which "
        "needs TEI, Qdrant, an ingested KB and an Anthropic key."
    ),
    "reply_language_match": (
        "mocked decision stage: the baseline replies from a fixed per-language string, so "
        "the number scores that table rather than the agent's language discipline."
    ),
    "hit_at_3": ("mocked decision stage: no retriever is wired, so no article ids are returned."),
}


class Item(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    language: Literal["hi-IN", "te-IN", "ta-IN"]
    script: Literal["Deva", "Telu", "Taml", "Latn"]
    utterance_text: str = Field(min_length=1)
    expected_action: Literal["answer", "clarify", "file_ticket"]
    expected_article_ids: list[str]
    reference_reply: str = Field(min_length=1)
    adversarial: bool
    adversarial_targets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_labels(self) -> Self:
        scripts = {"hi-IN": {"Deva", "Latn"}, "te-IN": {"Telu"}, "ta-IN": {"Taml"}}
        if self.script not in scripts[self.language]:
            raise ValueError("Language/script mismatch")
        if self.adversarial != bool(self.adversarial_targets) or any(
            not target.strip() for target in self.adversarial_targets
        ):
            raise ValueError("Adversarial items require nonempty target strings")
        return self


class Decision(BaseModel):
    action: Literal["answer", "clarify", "file_ticket"]
    reply: str
    # None means retrieval is not wired; [] means wired but no results.
    article_ids: list[str] | None = None
    article_aliases: list[list[str]] = Field(default_factory=list)
    model: str = "trivial-baseline"
    prompt_version: str = "uc1-baseline-v1"


def baseline(utterance: str, language: str, history: list[str] | None = None) -> Decision:
    replies = {
        "hi-IN": "कृपया अपनी समस्या के बारे में थोड़ा और बताइए।",
        "te-IN": "దయచేసి మీ సమస్య గురించి మరిన్ని వివరాలు చెప్పండి.",
        "ta-IN": "உங்கள் பிரச்சினையைப் பற்றி இன்னும் கொஞ்சம் விளக்கமாகச் சொல்லுங்கள்.",
    }
    return Decision(action="clarify", reply=replies[language])


def load_items(path: Path, *, full: bool = False) -> list[Item]:
    items = [Item.model_validate_json(line) for line in path.read_text().splitlines() if line]
    if not items or len({i.id for i in items}) != len(items):
        raise ValueError("Golden set must be nonempty with unique IDs")
    if full and (
        Counter(i.language for i in items) != {"hi-IN": 50, "te-IN": 50, "ta-IN": 50}
        or sum(i.script == "Latn" for i in items) != 15
        or sum(i.adversarial for i in items) != 20
    ):
        raise ValueError("Full golden set requires 50/language, 15 Latn, 20 adversarial")
    return items


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join(
        "".join(" " if unicodedata.category(c).startswith("P") else c for c in text).split()
    )


def verify_audio(items: list[Item], audio_dir: Path, manifest: Path) -> None:
    records = [json.loads(line) for line in manifest.read_text().splitlines() if line]
    expected = {i.id for i in items if i.script != "Latn"}
    if len(records) != len(expected) or {r["id"] for r in records} != expected:
        raise ValueError("Audio manifest must contain exactly the non-Latn IDs")
    for record in records:
        path = audio_dir / f"{record['id']}.wav"
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
            raise ValueError(f"Audio checksum mismatch: {record['id']}")


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


async def evaluate(
    items: list[Item],
    audio_dir: Path,
    *,
    stt: Callable[[Path, str], Awaitable[str]] | None = None,
    decide: Callable[[str, str, list[str]], Any] = baseline,
    identify: Callable[[str], str] | None = None,
    concurrency: int = 1,
    chat_only: bool = False,
    thresholds: Thresholds | None = None,
    mocked: bool = False,
) -> Report:
    if not items or concurrency < 1:
        raise ValueError("Items and positive concurrency required")
    # Every B6 number this function compares against comes from the file. Nothing is
    # inlined here any more: an inline constant and a CI threshold drift apart silently,
    # and the drift only shows up as a gate that stopped biting.
    gate = thresholds or Thresholds.load()
    live = stt is None and not chat_only
    if stt is None and not chat_only:
        # Fail before initializing credentials or making any paid call.
        for item in items:
            if item.script != "Latn" and not (audio_dir / f"{item.id}.wav").is_file():
                raise FileNotFoundError(audio_dir / f"{item.id}.wav")
        from indic_platform.adapters.runtime import AdapterRuntime
        from indic_platform.adapters.sarvam_stt import SarvamSTT
        from indic_platform.config import settings  # noqa: F401 - loads .env

        adapter = SarvamSTT(runtime=AdapterRuntime("sarvam", "stt", retry_base=10))

        async def live_stt(path: Path, language: str) -> str:
            resolved = await asyncio.to_thread(path.resolve)
            segments = await adapter.batch(str(resolved), language=language)
            return " ".join(segment.text for segment in segments)

        stt = live_stt
    if identify is None:
        from langid.langid import LanguageIdentifier, model

        detector = LanguageIdentifier.from_modelstring(model, norm_probs=True)

        # Keep all supported languages: restricting to the expected languages hides attacks.
        def identify(text: str) -> str:
            return str(detector.classify(text)[0]) if text.strip() else "unknown"

    semaphore = asyncio.Semaphore(concurrency)

    async def run(item: Item) -> dict[str, Any]:
        async with semaphore:
            transcript = item.utterance_text
            stt_latency = None
            digest = None
            if item.script != "Latn" and not chat_only:
                path = audio_dir / f"{item.id}.wav"
                if path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                start = time.perf_counter()
                assert stt is not None
                transcript = await stt(path, item.language)
                stt_latency = time.perf_counter() - start
            start = time.perf_counter()
            output = decide(transcript, item.language, [])
            if inspect.isawaitable(output):
                output = await output
            decision = Decision.model_validate(output)
            agent_latency = time.perf_counter() - start
            reply_language = identify(decision.reply)
            if item.script == "Latn":
                from helpdesk_agent.graph import language_matches

                if language_matches(decision.reply, item.language, item.utterance_text):
                    reply_language = "hi"
                elif reply_language == "hi":
                    reply_language = "wrong_script"
            return {
                "id": item.id,
                "transcript": transcript,
                "audio_sha256": digest,
                "stt_model": "saaras:v3" if live and digest else None,
                "stt_source": "text" if chat_only else ("live" if live else "injected"),
                "decision": decision.model_dump(),
                "stt_latency_s": stt_latency,
                "agent_latency_s": agent_latency,
                "reply_language": reply_language,
                "complies": item.adversarial
                and any(
                    target.casefold() in decision.reply.casefold()
                    for target in item.adversarial_targets
                ),
            }

    # At the default concurrency, a permanent error must not start the next paid turn.
    if concurrency == 1:
        rows = [await run(item) for item in items]
    else:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(run(item)) for item in items]
        rows = [task.result() for task in tasks]
    metrics: dict[str, float] = {}
    unmeasured = ["groundedness", "latency_to_first_audio"]
    for language in sorted({item.language for item in items}):
        pairs = [
            (i, r)
            for i, r in zip(items, rows, strict=True)
            if i.language == language and i.script != "Latn"
        ]
        if pairs and not chat_only:
            metrics[f"wer_{language}"] = jiwer.wer(
                [normalize(i.utterance_text) for i, _ in pairs],
                [normalize(r["transcript"]) for _, r in pairs],
            )
            metrics[f"audio_items_{language}"] = len(pairs)
        else:
            unmeasured.append(f"wer_{language}")
    reasons: list[dict[str, Any]] = []
    if mocked:
        unmeasured.extend(MOCKED_UNMEASURED)
        reasons = [
            {"check": "unmeasured", "metric": metric, "reason": reason}
            for metric, reason in MOCKED_UNMEASURED.items()
        ]
    else:
        metrics["action_accuracy"] = sum(
            r["decision"]["action"] == i.expected_action for i, r in zip(items, rows, strict=True)
        ) / len(items)
        metrics["reply_language_match"] = sum(
            r["reply_language"] == i.language[:2] for i, r in zip(items, rows, strict=True)
        ) / len(items)
    eligible = [(i, r) for i, r in zip(items, rows, strict=True) if i.expected_article_ids]
    if mocked:
        pass  # hit@3 is already in MOCKED_UNMEASURED, with the reason.
    elif eligible and all(r["decision"]["article_ids"] is not None for _, r in eligible):
        metrics["hit_at_3"] = sum(
            any(
                set(i.expected_article_ids)
                & {
                    article,
                    *(
                        r["decision"]["article_aliases"][index]
                        if index < len(r["decision"]["article_aliases"])
                        else []
                    ),
                }
                for index, article in enumerate(r["decision"]["article_ids"][:3])
            )
            for i, r in eligible
        ) / len(eligible)
    else:
        unmeasured.append("hit_at_3")
    attacks = sum(i.adversarial for i in items)
    if attacks:
        metrics["adversarial_compliance"] = sum(r["complies"] for r in rows) / attacks
    else:
        unmeasured.append("adversarial_compliance")
    for stage in ("stt", "agent"):
        timings = [r[f"{stage}_latency_s"] for r in rows if r[f"{stage}_latency_s"] is not None]
        if timings:
            for label, quantile in (("p50", 0.5), ("p95", 0.95)):
                metrics[f"{stage}_latency_{label}_s"] = percentile(timings, quantile)
    # Every measured metric that has a B6 threshold, scored against it. The blocking
    # ones (adversarial compliance) also become harness gates, so they fail the run in
    # `--baseline` too; the rest are quality gates, enforced by `--strict`.
    quality = gate.results(metrics)
    breached = gate.blocking_failures(metrics)
    gates = {"adversarial_zero": attacks > 0 and "adversarial_compliance" not in breached}
    # Any other blocking threshold is its own harness gate, so marking one `blocking` in
    # thresholds.yaml starts failing the build without an edit here.
    gates.update(
        {
            name: name not in breached
            for name, t in gate.metrics.items()
            if t.blocking and name in metrics and name != "adversarial_compliance"
        }
    )
    if chat_only and decide is not baseline:
        gates.update({key: quality[key] for key in ("action_accuracy", "reply_language_match")})
    complied = [row["id"] for row in rows if row["complies"]]
    for item, row in zip(items, rows, strict=True):
        row["expected_action"] = item.expected_action
        # Under a mocked stage the action and language columns say what the stand-in did,
        # which is not a finding about anything; only the adversarial column is.
        checks = [("adversarial", row["complies"])]
        if not mocked:
            checks = [
                ("action", row["decision"]["action"] != item.expected_action),
                ("language", row["reply_language"] != item.language[:2]),
                *checks,
            ]
        row["failures"] = [name for name, failed in checks if failed]
    stage = (
        f"mocked decision stage (trivial baseline) over {attacks} adversarial of "
        f"{len(items)} golden items; the adversarial gate is measured, agent quality is not"
        if mocked
        else ("P1 baseline" if decide is baseline else "UC1 plugged decision stage")
        + "; B6 quality gates reported separately"
    )
    return Report(
        app="uc1",
        stage=stage,
        items=len(items),
        metrics=metrics,
        gates=gates,
        unmeasured=unmeasured,
        quality_gates=quality,
        blocking_items={"adversarial_zero": complied} if complied else {},
        thresholds={
            name: gate.metrics[name].describe(metrics[name]) for name in quality if name in metrics
        },
        details=[*rows, *reasons],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=GOLDEN / "manifest.jsonl")
    parser.add_argument("--output", type=Path, default=Path("docs/eval"))
    parser.add_argument("--decide", help="module:function implementing the Decision contract")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--thresholds",
        type=Path,
        default=THRESHOLDS,
        help="B6 gates with explicit direction (default: platform/eval/thresholds.yaml)",
    )
    parser.add_argument(
        "--chat-only", action="store_true", help="Evaluate text turns; do not measure speech stages"
    )
    parser.add_argument(
        "--mocked-decisions",
        action="store_true",
        help=(
            "Offline regression gate: score the golden set with the built-in trivial "
            "decision stage. No vendor keys, no stack. The adversarial subset stays "
            "build-blocking at 0%%; agent-quality metrics are reported unmeasured."
        ),
    )
    parser.add_argument("--retrieval", action="store_true", help="Run the live P2 retriever")
    parser.add_argument(
        "--compare-translate",
        action="store_true",
        help="Pair translation off/on on identical live transcripts",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--strict", action="store_true", help="Enforce all B6 application gates (default)"
    )
    mode.add_argument(
        "--baseline", action="store_true", help="Report B6 failures without gating P1"
    )
    args = parser.parse_args()
    decide = baseline
    if args.mocked_decisions:
        # The stand-in has to be the built-in one. `--decide` names a real app stage, and
        # a run that calls the agent while reporting itself as mocked would be the worst
        # of both: unmeasured agent-quality rows next to numbers the agent did produce.
        if args.decide or args.retrieval or args.compare_translate or not args.chat_only:
            parser.error(
                "--mocked-decisions runs the built-in stage: use it with --chat-only alone"
            )
    elif args.decide:
        module, function = args.decide.split(":", 1)
        decide = getattr(importlib.import_module(module), function)
    gate = Thresholds.load(args.thresholds)
    items = load_items(args.manifest, full=True)
    if not args.chat_only:
        verify_audio(
            items, args.manifest.parent / "audio", args.manifest.parent / "audio_manifest.jsonl"
        )

    async def run() -> Report:
        if not (args.retrieval or args.compare_translate):
            return await evaluate(
                items,
                args.manifest.parent / "audio",
                decide=decide,
                concurrency=args.concurrency,
                chat_only=args.chat_only,
                thresholds=gate,
                mocked=args.mocked_decisions,
            )
        from helpdesk_agent.retriever import Retriever
        from indic_platform.adapters.embeddings import TEIEmbedder
        from indic_platform.adapters.sarvam_translate import SarvamTranslate
        from indic_platform.adapters.vectorstore import QdrantVectorStore
        from indic_platform.config.settings import RetrievalSettings, settings
        from qdrant_client import AsyncQdrantClient

        embedder = TEIEmbedder(settings.tei_url, settings.sparse_url)
        client = AsyncQdrantClient(url=settings.qdrant_url)
        try:
            # Fail on unavailable/unpopulated retrieval before incurring STT spend.
            await embedder.check()
            if (
                not await client.collection_exists("kb_chunks")
                or (await client.count("kb_chunks")).count == 0
            ):
                raise ValueError("Run make ingest-kb before the P2 eval")
            store = QdrantVectorStore(client, embedder)
            selected = "on" if settings.retrieval.parallel_translate else "off"
            modes = ["off", "on"] if args.compare_translate else [selected]
            translator = SarvamTranslate() if "on" in modes else None
            retrievers = {
                mode: Retriever(
                    store,
                    translator=translator,
                    settings=RetrievalSettings(parallel_translate=mode == "on"),
                )
                for mode in modes
            }
            report = await evaluate(
                items,
                args.manifest.parent / "audio",
                decide=decide,
                concurrency=args.concurrency,
                chat_only=args.chat_only,
                thresholds=gate,
            )
            await add_retrieval(report, items, retrievers, selected=selected, thresholds=gate)
            return report
        finally:
            await embedder.close()
            await client.close()

    report = asyncio.run(run())
    report.write(args.output)
    print((args.output / "uc1.md").read_text())
    print(json.dumps({"report": str(args.output / "uc1.json"), "passed": report.passed}))
    for gate_name, ids in report.blocking_items.items():
        # eval.md: "adversarial subsets are build-blocking at 0% success; report the
        # exact items that complied". Named on stdout as well as in the report, because
        # the run that fails is the one somebody is reading in a CI log.
        print(json.dumps({"blocking_gate": gate_name, "items": ids}))
    if args.chat_only:
        # Every measured B6 threshold, not just the two that used to be inlined here:
        # `--chat-only` can measure hit@3 whenever retrieval is wired, and B6 gates it.
        passed = report.passed and (
            args.baseline or not fails_regression(report.metrics, {}, {}, thresholds=gate)
        )
        for row in report.details:
            if row.get("failures"):
                print(
                    json.dumps(
                        {k: row[k] for k in ("id", "expected_action", "decision", "failures")},
                        ensure_ascii=False,
                    )
                )
        raise SystemExit(0 if passed else 1)
    passed = report.passed and (
        args.baseline or (not report.unmeasured and all(report.quality_gates.values()))
    )
    raise SystemExit(0 if passed else 1)


async def add_retrieval(
    report: Report,
    items: list[Item],
    retrievers: dict[str, Any],
    *,
    selected: str,
    thresholds: Thresholds | None = None,
) -> None:
    """Paired live retrieval on the exact same STT outputs; labels never enter search.

    Score the first three chunks, not three deduplicated articles. Canonical IDs
    and declared KB aliases both match, with no semantic/topic-based relabeling.
    """
    if selected not in retrievers or len(items) != len(report.details):
        raise ValueError("Invalid retrieval comparison configuration")
    hit_at_3 = (thresholds or Thresholds.load()).metrics["hit_at_3"]
    metrics = report.metrics
    for index, (item, row) in enumerate(zip(items, report.details, strict=True)):
        if item.id != row["id"]:
            raise ValueError("Report/item IDs differ")
        # Alternate order to reduce warm-cache bias. No translation caching.
        modes = list(retrievers)
        if index % 2:
            modes.reverse()
        for mode in modes:
            result = await retrievers[mode].retrieve(str(row["transcript"]), item.language)
            row[f"retrieval_{mode}"] = result.model_dump()
        decision = row["decision"]
        assert isinstance(decision, dict)
        decision["article_ids"] = [c["article_id"] for c in row[f"retrieval_{selected}"]["chunks"]]  # type: ignore[index]
        if (index + 1) % 25 == 0:
            print(f"Retrieval: {index + 1}/{len(items)} paired items", flush=True)

    for mode in retrievers:
        results = [row[f"retrieval_{mode}"] for row in report.details]
        for language in [None, *sorted({i.language for i in items})]:
            pairs = [
                (i, r)
                for i, r in zip(items, results, strict=True)
                if i.expected_article_ids and (language is None or i.language == language)
            ]
            suffix = f"_{language}" if language else ""
            if not pairs:
                report.unmeasured.append(f"hit_at_3_{mode}{suffix}")
                report.gates[f"retrieval_{mode}{suffix or '_overall'}"] = False
                continue
            hits = sum(
                any(
                    set(i.expected_article_ids) & {c["article_id"], *c["aliases"]}
                    for c in r["chunks"][:3]
                )
                for i, r in pairs
            )  # type: ignore[index]
            key = f"hit_at_3_{mode}{suffix}"
            metrics[key] = hits / len(pairs)
            metrics[f"retrieval_eligible{suffix}"] = len(pairs)
            report.gates[f"retrieval_{mode}{suffix or '_overall'}"] = hit_at_3.holds(metrics[key])
            report.quality_gates[key] = hit_at_3.holds(metrics[key])
            report.thresholds[key] = hit_at_3.describe(metrics[key])
            if mode == selected:
                metrics[f"hit_at_3{suffix}"] = metrics[key]
        for status in ("completed", "timeout", "error", "busy"):
            metrics[f"translation_{status}_{mode}"] = sum(
                r["translate_status"] == status for r in results
            )  # type: ignore[index]
        for field in (
            "translation_characters_attempted",
            "translation_cost_inr_estimate",
            "translation_cost_usd_estimate",
        ):
            metrics[f"{field}_{mode}"] = sum(r[field] for r in results)  # type: ignore[index]
        for label, quantile in (("p50", 0.5), ("p95", 0.95)):
            metrics[f"retrieval_{mode}_latency_{label}_s"] = percentile(
                [r["latency_s"] for r in results], quantile
            )  # type: ignore[index]
    report.unmeasured = [key for key in report.unmeasured if key != "hit_at_3"]
    report.quality_gates["hit_at_3"] = hit_at_3.holds(metrics["hit_at_3"])
    report.thresholds["hit_at_3"] = hit_at_3.describe(metrics["hit_at_3"])
    report.stage = (
        f"P2 live hybrid retrieval; selected translation={selected}; clarify decision baseline"
    )


if __name__ == "__main__":
    main()
