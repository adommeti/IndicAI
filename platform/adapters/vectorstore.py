from collections import defaultdict
from typing import Any, Literal, Protocol

from indic_platform.adapters.embeddings import Embedder
from indic_platform.adapters.runtime import AdapterRuntime
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, models


class Chunk(BaseModel):
    id: str
    article_id: str
    title: str
    category: str
    lang: Literal["en"] = "en"
    text: str
    aliases: list[str] = Field(default_factory=list)
    score: float = 0


class SearchHit(BaseModel):
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(Protocol):
    async def search(self, query_text: str, k: int, hybrid: bool = True) -> list[Chunk]: ...


def rrf(rankings: list[list[Chunk]], *, k: int = 6) -> list[Chunk]:
    scores: dict[str, float] = defaultdict(float)
    chunks = {}
    for ranking in rankings:
        seen = set()
        for rank, chunk in enumerate(ranking, start=1):
            if chunk.id not in seen:
                scores[chunk.id] += 1 / (60 + rank)
                chunks[chunk.id] = chunk
                seen.add(chunk.id)
    return [
        chunks[id].model_copy(update={"score": scores[id]})
        for id in sorted(scores, key=lambda id: (-scores[id], id))[:k]
    ]


class QdrantVectorStore:
    def __init__(
        self,
        client: AsyncQdrantClient,
        embedder: Embedder,
        *,
        collection: str = "kb_chunks",
        runtime: AdapterRuntime | None = None,
    ) -> None:
        self.client, self.embedder, self.collection = client, embedder, collection
        self.runtime = runtime or AdapterRuntime("qdrant", "retrieval", timeout=120)

    async def ensure_collection(self) -> None:
        async def operation() -> None:
            if not await self.client.collection_exists(self.collection):
                await self.client.create_collection(
                    self.collection,
                    vectors_config={
                        "dense": models.VectorParams(size=1024, distance=models.Distance.COSINE)
                    },
                    sparse_vectors_config={"sparse": models.SparseVectorParams()},
                )
            info = await self.client.get_collection(self.collection)
            vectors = info.config.params.vectors
            sparse = info.config.params.sparse_vectors or {}
            if (
                not isinstance(vectors, dict)
                or "dense" not in vectors
                or vectors["dense"].size != 1024
                or vectors["dense"].distance != models.Distance.COSINE
                or "sparse" not in sparse
            ):
                raise ValueError(
                    "Incompatible kb_chunks collection; no automatic destructive recreation"
                )

        await self.runtime.call(operation, model="qdrant-local", units={"operations": 1})

    async def upsert_article(self, chunks: list[Chunk]) -> None:
        if not chunks or len({c.article_id for c in chunks}) != 1:
            raise ValueError("Expected nonempty chunks for exactly one article")
        embeddings = await self.embedder.embed([c.text for c in chunks])
        if len(embeddings) != len(chunks):
            raise ValueError("Embedding count mismatch")
        points = [
            models.PointStruct(
                id=c.id,
                vector={
                    "dense": e.dense,
                    "sparse": models.SparseVector(
                        indices=list(e.sparse), values=list(e.sparse.values())
                    ),
                },
                payload=c.model_dump(exclude={"id", "score"}),
            )
            for c, e in zip(chunks, embeddings, strict=True)
        ]

        async def operation() -> None:
            await self.client.upsert(self.collection, points=points, wait=True)
            # Remove only superseded chunks of this article after successful upsert.
            await self.client.delete(
                self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="article_id",
                                match=models.MatchValue(value=chunks[0].article_id),
                            )
                        ],
                        must_not=[models.HasIdCondition(has_id=[c.id for c in chunks])],
                    )
                ),
                wait=True,
            )

        await self.runtime.call(operation, model="qdrant-local", units={"operations": 2})

    async def search(self, query_text: str, k: int, hybrid: bool = True) -> list[Chunk]:
        if k < 1:
            raise ValueError("k must be positive")
        embedding = (await self.embedder.embed([query_text], sparse=hybrid))[0]

        async def operation() -> list[Chunk]:
            if hybrid and embedding.sparse:
                response = await self.client.query_points(
                    self.collection,
                    prefetch=[
                        models.Prefetch(query=embedding.dense, using="dense", limit=max(20, k)),
                        models.Prefetch(
                            query=models.SparseVector(
                                indices=list(embedding.sparse),
                                values=list(embedding.sparse.values()),
                            ),
                            using="sparse",
                            limit=max(20, k),
                        ),
                    ],
                    query=models.FusionQuery(fusion=models.Fusion.RRF),
                    limit=k,
                    with_payload=True,
                )
            else:
                response = await self.client.query_points(
                    self.collection,
                    query=embedding.dense,
                    using="dense",
                    limit=k,
                    with_payload=True,
                )
            return [
                Chunk.model_validate({**(p.payload or {}), "id": str(p.id), "score": p.score})
                for p in response.points
            ]

        return await self.runtime.call(operation, model="qdrant-local", units={"operations": 1})
