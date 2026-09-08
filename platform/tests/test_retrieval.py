import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from helpdesk_agent.ingest import Article, chunk_article, load_articles
from helpdesk_agent.retriever import Retriever
from indic_platform.adapters.embeddings import Embedding
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.adapters.vectorstore import Chunk, QdrantVectorStore, rrf
from indic_platform.config.settings import RetrievalSettings
from indic_platform.obs.langfuse import MemorySink
from qdrant_client import AsyncQdrantClient
from tokenizers import Tokenizer, models, pre_tokenizers


def chunk(id: str, article: str = "VPN-001") -> Chunk:
    return Chunk(id=id, article_id=article, title="VPN", category="IT", text="Connect VPN")


def test_rrf_deduplicates_and_scores() -> None:
    result = rrf([[chunk("a"), chunk("b")], [chunk("b"), chunk("c")]], k=3)
    assert [c.id for c in result] == ["b", "a", "c"]
    assert result[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert rrf([[chunk("a"), chunk("a")]], k=3)[0].score == 1 / 61


async def test_translate_deadline_cancels_slow_pass() -> None:
    cancelled = asyncio.Event()

    async def slow(text: str, *, source: str, target: str) -> str:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return "VPN"

    store = AsyncMock()
    store.search.return_value = [chunk("original")]
    translator = AsyncMock()
    translator.translate.side_effect = slow
    retriever = Retriever(
        store, translator=translator, settings=RetrievalSettings(parallel_translate=True)
    )
    started = asyncio.get_running_loop().time()
    result = await retriever.retrieve("वीपीएन", "hi-IN")
    assert asyncio.get_running_loop().time() - started < 0.5
    assert result.translate_status == "timeout"
    assert [c.id for c in result.chunks] == ["original"]
    assert cancelled.is_set()
    store.search.assert_awaited_once_with("वीपीएन", 6, hybrid=True)


async def test_translate_search_is_also_deadlined() -> None:
    async def search(text: str, k: int, hybrid: bool = True) -> list[Chunk]:
        if text == "English":
            await asyncio.sleep(10)
        return [chunk("original")]

    store = AsyncMock()
    store.search.side_effect = search
    translator = AsyncMock()
    translator.translate.return_value = "English"
    result = await Retriever(
        store, translator=translator, settings=RetrievalSettings(parallel_translate=True)
    ).retrieve("விபிஎன்", "ta-IN")
    assert result.translate_status == "timeout"
    assert result.chunks[0].id == "original"


async def test_noncooperative_translation_cannot_extend_deadline() -> None:
    release = asyncio.Event()

    async def ignores_cancel(*args: object, **kwargs: object) -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()
        return "late translation"

    store = AsyncMock()
    store.search.return_value = [chunk("original")]
    translator = AsyncMock()
    translator.translate.side_effect = ignores_cancel
    retriever = Retriever(
        store, translator=translator, settings=RetrievalSettings(parallel_translate=True)
    )
    start = asyncio.get_running_loop().time()
    result = await retriever.retrieve("वीपीएन", "hi-IN")
    assert asyncio.get_running_loop().time() - start < 0.5
    assert result.translate_status == "timeout"
    assert result.chunks[0].id == "original"
    release.set()
    if retriever._late_tasks:
        await asyncio.gather(*retriever._late_tasks, return_exceptions=True)
    # A late translation must not start an additional embedding/search request.
    store.search.assert_awaited_once()


async def test_translate_fuses_and_off_makes_no_call() -> None:
    store = AsyncMock()
    store.search.side_effect = [[chunk("a"), chunk("b")], [chunk("b")]]
    translator = AsyncMock()
    translator.translate.return_value = "VPN"
    result = await Retriever(
        store, translator=translator, settings=RetrievalSettings(parallel_translate=True)
    ).retrieve("వీపీఎన్", "te-IN")
    assert result.chunks[0].id == "b"
    assert result.translate_status == "completed"
    translator.translate.assert_awaited_once_with("వీపీఎన్", source="te-IN", target="en-IN")
    translator.reset_mock()
    store.search.side_effect = None
    store.search.return_value = []
    await Retriever(store, translator=translator, settings=RetrievalSettings()).retrieve(
        "VPN", "hi-IN"
    )
    translator.translate.assert_not_awaited()


async def test_stuck_cancelled_translation_work_is_bounded() -> None:
    release = asyncio.Event()

    async def stuck(*args: object, **kwargs: object) -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()
        return "late"

    store = AsyncMock()
    store.search.return_value = [chunk("original")]
    translator = AsyncMock()
    translator.translate.side_effect = stuck
    retriever = Retriever(
        store,
        translator=translator,
        settings=RetrievalSettings(parallel_translate=True, translate_deadline_s=0.01),
    )
    results = [await retriever.retrieve("VPN", "hi-IN") for _ in range(8)]
    assert [r.translate_status for r in results] == ["timeout"] * 4 + ["busy"] * 4
    assert len(retriever._late_tasks) == 4
    assert translator.translate.await_count == 4
    release.set()
    await asyncio.gather(*retriever._late_tasks, return_exceptions=True)


def test_load_markdown_html_and_duplicate_ids(tmp_path: Path) -> None:
    (tmp_path / "vpn.md").write_text(
        "---\nid: VPN-001\ncategory: IT\naliases: [kb-vpn]\n---\n# VPN\n## Connect\nUse VPN."
    )
    (tmp_path / "leave.html").write_text(
        '<html><head><meta name="article_id" content="HR-001">'
        '<meta name="category" content="HR"></head><body><h1>Leave</h1>'
        "<h2>Apply</h2><p>Ask manager.</p><script>bad()</script></body></html>"
    )
    articles = load_articles(tmp_path)
    assert len(articles) == 2
    assert articles[0].title == "Leave"
    assert "bad()" not in articles[0].text
    assert "## Apply" in articles[0].text
    (tmp_path / "duplicate.md").write_text((tmp_path / "vpn.md").read_text())
    with pytest.raises(ValueError, match="Duplicate"):
        load_articles(tmp_path)


def test_chunk_budget_overlap_and_stable_ids() -> None:
    tokenizer = Tokenizer(
        models.WordLevel({"[UNK]": 0, **{f"w{i}": i + 1 for i in range(1100)}}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    article = Article(
        id="VPN-001",
        title="VPN",
        category="IT",
        text="# VPN\n## Connect\n" + " ".join(f"w{i}" for i in range(1100)),
    )
    chunks = chunk_article(article, tokenizer)
    assert len(chunks) == 3
    assert all(len(tokenizer.encode(c.text).ids) <= 500 for c in chunks)
    assert all("## Connect" in c.text for c in chunks)
    first, second = [c.text.split("\n\n", 1)[1].split() for c in chunks[:2]]
    assert first[-60:] == second[:60]
    assert [c.id for c in chunks] == [c.id for c in chunk_article(article, tokenizer)]


async def test_qdrant_hybrid_idempotency_and_stale_chunks() -> None:
    client = AsyncQdrantClient(":memory:")
    embedder = AsyncMock()
    embedder.embed.side_effect = lambda texts, **kwargs: [
        Embedding(dense=[1.0] + [0.0] * 1023, sparse={7: 0.9}) for _ in texts
    ]
    store = QdrantVectorStore(
        client, embedder, runtime=AdapterRuntime("qdrant", "retrieval", sink=MemorySink())
    )
    await store.ensure_collection()
    import uuid

    a, b = chunk(str(uuid.uuid4())), chunk(str(uuid.uuid4()))
    await store.upsert_article([a, b])
    await store.upsert_article([a, b])
    assert (await client.count("kb_chunks")).count == 2
    await store.upsert_article([a])
    assert (await client.count("kb_chunks")).count == 1
    result = await store.search("VPN", 3)
    assert result[0].id == a.id
    assert result[0].score > 0
    assert (await store.search("VPN", 3, hybrid=False))[0].article_id == "VPN-001"
    await client.close()


async def test_paired_eval_aliases_denominators_and_gate(tmp_path: Path) -> None:
    from helpdesk_agent.retriever import RetrievalResult
    from indic_platform.eval.runners.run_uc1 import add_retrieval, evaluate, load_items

    items = load_items(Path(__file__).parent / "fixtures/uc1.jsonl")
    report = await evaluate(
        items, tmp_path, stt=AsyncMock(return_value="recognized"), identify=lambda _: "hi"
    )
    retrievers = {}
    for mode, alias in [("off", "vpn"), ("on", "wrong")]:
        retriever = AsyncMock()
        hit = chunk("a").model_copy(update={"aliases": [alias]})
        retriever.retrieve.return_value = RetrievalResult(
            chunks=[hit],
            translate_status="disabled" if mode == "off" else "timeout",
            latency_s=0.1,
            original_latency_s=0.1,
        )
        retrievers[mode] = retriever
    await add_retrieval(report, items, retrievers, selected="off")
    assert report.metrics["hit_at_3_off"] == 1
    assert report.metrics["hit_at_3_on"] == 0
    assert report.metrics["hit_at_3_off_hi-IN"] == 1
    assert report.metrics["hit_at_3"] == 1
    assert "hit_at_3" not in report.unmeasured
    assert report.gates["retrieval_off_overall"]
    assert not report.gates["retrieval_on_overall"]
    assert retrievers["off"].retrieve.await_args_list[0].args == ("recognized", "hi-IN")
    retrievers["on"].retrieve.return_value.translate_status = "busy"
    await add_retrieval(report, items, retrievers, selected="off")
    assert report.metrics["translation_busy_on"] == len(items)
    assert report.metrics["translation_timeout_on"] == 0


def test_sample_kb_contract() -> None:
    from helpdesk_agent.ingest import SAMPLE_KB
    from indic_platform.eval.runners.run_uc1 import GOLDEN, load_items

    articles = load_articles(SAMPLE_KB)
    assert len(articles) == 25
    ids = {id for a in articles for id in [a.id, *a.aliases]}
    assert all(set(i.expected_article_ids) <= ids for i in load_items(GOLDEN / "manifest.jsonl"))
