from typing import Any, Protocol

from pydantic import BaseModel


class SearchHit(BaseModel):
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(Protocol):
    async def search(
        self, vector: list[float], *, limit: int = 3, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]: ...
