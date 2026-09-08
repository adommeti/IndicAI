"""Idempotent, per-article Markdown/HTML KB ingestion."""

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from indic_platform.adapters.embeddings import MODEL, REVISION, TEIEmbedder
from indic_platform.adapters.vectorstore import Chunk, QdrantVectorStore
from indic_platform.config.settings import settings
from indic_platform.security.redact import redact
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from tokenizers import Tokenizer

SAMPLE_KB = Path(__file__).parent / "sample_kb"


class Article(BaseModel):
    id: str = Field(pattern=r"^[A-Z]+-\d{3}$")
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    text: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


def load_articles(folder: Path) -> list[Article]:
    articles = []
    seen: set[str] = set()
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".html", ".htm"}:
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".md":
            match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, re.S)
            if not match:
                raise ValueError(f"Missing YAML article metadata: {path.name}")
            metadata = yaml.safe_load(match[1])
            text = match[2]
        else:
            soup = BeautifulSoup(text, "html.parser")
            metadata = {
                str(m.get("name")): str(m.get("content", "")) for m in soup.find_all("meta")
            }
            metadata["id"] = metadata.pop("article_id", metadata.get("id", ""))
            for element in soup(["script", "style", "head", "nav"]):
                element.decompose()
            for heading in soup.find_all(re.compile(r"^h[1-6]$")):
                heading.replace_with(
                    "\n"
                    + "#" * int(heading.name[1])
                    + " "
                    + heading.get_text(" ", strip=True)
                    + "\n"
                )
            text = soup.get_text("\n", strip=True)
        title = re.search(r"^#\s+(.+)$", text, re.M)
        metadata.setdefault("title", title[1].strip() if title else "")
        article = Article.model_validate({**metadata, "text": text})
        identities = {article.id, *article.aliases}
        if seen & identities or len(identities) != 1 + len(article.aliases):
            raise ValueError(f"Duplicate article ID or alias: {path.name}")
        seen.update(identities)
        articles.append(article)
    if not articles:
        raise ValueError("No Markdown/HTML KB articles found")
    return articles


def chunk_article(
    article: Article, tokenizer: Tokenizer, *, size: int = 500, overlap: int = 60
) -> list[Chunk]:
    """Repeat the heading hierarchy; body windows overlap by exactly 60 model tokens.

    Heading tokens count toward the budget. Offsets slice original text without
    decoding/re-tokenization artifacts. No chunk crosses a heading boundary.
    """
    if overlap < 0 or size <= overlap:
        raise ValueError("Invalid chunk size/overlap")
    headings: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            prefix = "\n".join(h for _, h in headings) or f"# {article.title}"
            sections.append((prefix, body))
        lines.clear()

    for line in redact(article.text).splitlines():
        match = re.match(r"^(#{1,6})\s+", line)
        if match:
            flush()
            level = len(match[1])
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, line))
        else:
            lines.append(line)
    flush()
    chunks = []
    for section_index, (prefix, body) in enumerate(sections):
        # Reserve two special tokens used by TEI's tokenizer.
        capacity = size - len(tokenizer.encode(prefix, add_special_tokens=False).ids) - 2
        if capacity <= overlap:
            raise ValueError("Heading hierarchy leaves no room for chunk body")
        offsets = tokenizer.encode(body, add_special_tokens=False).offsets
        start = 0
        while start < len(offsets):
            end = min(start + capacity, len(offsets))
            text = prefix + "\n\n" + body[offsets[start][0] : offsets[end - 1][1]]
            while len(tokenizer.encode(text).ids) > size and end > start + overlap:
                end -= 1
                text = prefix + "\n\n" + body[offsets[start][0] : offsets[end - 1][1]]
            if len(tokenizer.encode(text).ids) > size:
                raise ValueError("Unable to fit chunk into token budget")
            digest = hashlib.sha256(text.encode()).hexdigest()
            id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"kb-v1:{article.id}:{section_index}:{start}:{digest}"
                )
            )
            chunks.append(
                Chunk(
                    id=id,
                    article_id=article.id,
                    title=article.title,
                    category=article.category,
                    text=text,
                    aliases=article.aliases,
                )
            )
            if end == len(offsets):
                break
            start = end - overlap
    if not chunks:
        raise ValueError(f"Article has no body: {article.id}")
    return chunks


async def ingest(folder: Path = SAMPLE_KB) -> dict[str, int]:
    articles = load_articles(folder)
    tokenizer = await asyncio.to_thread(Tokenizer.from_pretrained, MODEL, revision=REVISION)
    groups = [chunk_article(article, tokenizer) for article in articles]
    embedder = TEIEmbedder(settings.tei_url, settings.sparse_url)
    client = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await embedder.check()
        store = QdrantVectorStore(client, embedder)
        await store.ensure_collection()
        for chunks in groups:
            await store.upsert_article(chunks)
        return {
            "articles": len(articles),
            "chunks": sum(map(len, groups)),
            "collection_points": (await client.count("kb_chunks", exact=True)).count,
        }
    finally:
        await embedder.close()
        await client.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(ingest())))
