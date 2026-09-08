import asyncio
import time
from typing import Literal, Protocol

from indic_platform.adapters.embeddings import MODEL, REVISION
from indic_platform.adapters.vectorstore import Chunk, VectorStore, rrf
from indic_platform.config.settings import RetrievalSettings
from indic_platform.config.settings import settings as app_settings
from indic_platform.obs.langfuse import cost
from indic_platform.security.redact import redact
from pydantic import BaseModel, Field


class Translator(Protocol):
    async def translate(self, text: str, *, source: str, target: str) -> str: ...


class RetrievalResult(BaseModel):
    embedding_model: str = MODEL
    embedding_revision: str = REVISION
    chunks: list[Chunk]
    original_chunks: list[Chunk] = Field(default_factory=list)
    translate_status: Literal["disabled", "completed", "timeout", "error", "busy"]
    latency_s: float
    original_latency_s: float
    translate_latency_s: float | None = None
    translation_characters_attempted: int = 0
    # Cancelled requests may still be billed upstream; estimate, not an invoice.
    translation_cost_inr_estimate: float = 0
    translation_cost_usd_estimate: float = 0


class Retriever:
    def __init__(
        self,
        store: VectorStore,
        *,
        translator: Translator | None = None,
        settings: RetrievalSettings | None = None,
    ) -> None:
        self.store, self.translator = store, translator
        self.settings = settings or app_settings.retrieval
        self._late_tasks: set[asyncio.Task[list[Chunk]]] = set()
        self._translation_slots = asyncio.Semaphore(4)

    def _discard_later(self, task: asyncio.Task[list[Chunk]]) -> None:
        """Retain/observe cancelled work without letting cleanup extend the deadline."""
        self._late_tasks.add(task)

        def consume(done: asyncio.Task[list[Chunk]]) -> None:
            self._late_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(consume)
        task.cancel()

    async def retrieve(self, query: str, language: str) -> RetrievalResult:
        start = time.perf_counter()
        translated: list[Chunk] = []
        status: Literal["disabled", "completed", "timeout", "error", "busy"] = "disabled"
        translate_latency = None
        characters = 0

        async def original_pass() -> tuple[list[Chunk], float]:
            chunks = await self.store.search(query, 6, hybrid=True)
            return chunks, time.perf_counter() - start

        async def translate_pass() -> None:
            nonlocal translated, status, translate_latency, characters
            branch_start = time.perf_counter()
            deadline = branch_start + self.settings.translate_deadline_s
            if self._translation_slots.locked():
                status, translate_latency = "busy", 0.0
                return
            await self._translation_slots.acquire()

            async def work() -> list[Chunk]:
                nonlocal characters
                if self.translator is None:
                    raise ValueError("Parallel translation requires a translator")
                characters = len(redact(query))
                text = await self.translator.translate(query, source=language, target="en-IN")
                if time.perf_counter() >= deadline:
                    raise TimeoutError("Translation arrived after deadline")
                return await self.store.search(text, 6, hybrid=True)

            task = asyncio.create_task(work())
            # A non-cooperative cancelled task retains its slot until truly done.
            task.add_done_callback(lambda _: self._translation_slots.release())
            try:
                done, _ = await asyncio.wait({task}, timeout=max(0, deadline - time.perf_counter()))
                if not done or time.perf_counter() >= deadline:
                    status = "timeout"
                else:
                    translated = task.result()
                    status = "completed"
            except TimeoutError:
                status = "timeout"
            except Exception:
                status = "error"
            finally:
                if not task.done():
                    self._discard_later(task)
                elif not task.cancelled():
                    task.exception()
                translate_latency = time.perf_counter() - branch_start

        async with asyncio.TaskGroup() as group:
            original = group.create_task(original_pass())
            if self.settings.parallel_translate:
                group.create_task(translate_pass())
        chunks, original_latency = original.result()
        inr, usd = cost("mayura:v1", {"characters": characters})
        return RetrievalResult(
            chunks=rrf([chunks, translated]) if translated else chunks,
            original_chunks=chunks,
            translate_status=status,
            latency_s=time.perf_counter() - start,
            original_latency_s=original_latency,
            translate_latency_s=translate_latency,
            translation_characters_attempted=characters,
            translation_cost_inr_estimate=inr,
            translation_cost_usd_estimate=usd,
        )
