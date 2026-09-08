"""TEI dense + BGE-M3 learned sparse sidecar. No lexical/BM25 fallback."""

import asyncio
import math
from typing import Protocol, Self

import httpx
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.security.redact import redact
from pydantic import BaseModel, Field, model_validator

MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


class Embedding(BaseModel):
    dense: list[float] = Field(min_length=1024, max_length=1024)
    sparse: dict[int, float]

    @model_validator(mode="after")
    def valid_vectors(self) -> Self:
        if not all(math.isfinite(v) for v in self.dense + list(self.sparse.values())):
            raise ValueError("Non-finite embedding")
        if any(k < 0 or v <= 0 for k, v in self.sparse.items()):
            raise ValueError("Invalid sparse weights")
        return self


class Embedder(Protocol):
    async def embed(self, texts: list[str], *, sparse: bool = True) -> list[Embedding]: ...


class TEIEmbedder:
    def __init__(
        self,
        tei_url: str,
        sparse_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        runtime: AdapterRuntime | None = None,
    ) -> None:
        self.tei_url, self.sparse_url = tei_url.rstrip("/"), sparse_url.rstrip("/")
        self.client = client or httpx.AsyncClient(timeout=120)
        self.runtime = runtime or AdapterRuntime("tei", "embedding", timeout=120)

    async def check(self) -> None:
        responses = await asyncio.gather(
            self.client.get(f"{self.tei_url}/info"), self.client.get(f"{self.sparse_url}/info")
        )
        for response in responses:
            response.raise_for_status()
            if response.json()["model_id"] != MODEL:
                raise ValueError("Embedding server must serve BAAI/bge-m3")
        if responses[1].json().get("model_sha") != REVISION:
            raise ValueError("Sparse server model revision mismatch")
        dense_revision = responses[0].json().get("model_sha")
        if dense_revision is not None and dense_revision != REVISION:
            raise ValueError("Dense server model revision mismatch")

    async def embed(self, texts: list[str], *, sparse: bool = True) -> list[Embedding]:
        result = []
        # One chunk/request fits TEI's 512-token batch budget. Reject truncation.
        for text in texts:
            clean = redact(text)

            async def operation(clean: str = clean) -> Embedding:
                requests = [
                    self.client.post(
                        f"{self.tei_url}/embed", json={"inputs": clean, "truncate": False}
                    )
                ]
                if sparse:
                    requests.append(
                        self.client.post(
                            f"{self.sparse_url}/embed_sparse", json={"inputs": [clean]}
                        )
                    )
                responses = await asyncio.gather(*requests)
                for response in responses:
                    response.raise_for_status()
                return Embedding(
                    dense=responses[0].json()[0], sparse=responses[1].json()[0] if sparse else {}
                )

            result.append(
                await self.runtime.call(operation, model=MODEL, units={"characters": len(clean)})
            )
        return result

    async def close(self) -> None:
        await self.client.aclose()
