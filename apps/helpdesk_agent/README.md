# Helpdesk retrieval (P2)

The FastAPI package remains a health-only scaffold. No redaction override is
enabled; all vendor access goes through `indic_platform.adapters`.

The 25 sample articles describe a fictional employer, not actual corporate policy.
Seven articles explicitly declare the legacy P1 `kb-*` identifiers as aliases;
canonical IDs follow `VPN-001` style. Aliases are metadata, not query heuristics.
Golden labels are unchanged. Native-language editorial review remains pending.

`make ingest-kb` loads the bundled samples. For another folder use
`uv run python -m indic_platform.cli ingest-kb --folder /path/to/kb`.
Markdown requires YAML front matter with `id`, `category`, optional `title` and
`aliases`, plus an H1 title. HTML uses `meta name="article_id"`,
`meta name="category"`, and an H1; scripts/styles/navigation are excluded.

Chunks use the pinned bge-m3 tokenizer, a maximum 500-token budget including
heading hierarchy and special tokens, and 60 body-token overlap within a section.
Short articles remain short; headings are not padded to reach 500 tokens.
UUIDs derive from article, section, offset and content hash. Reingestion upserts
the same IDs and removes only superseded chunks of that article after success.
Run one ingestion writer at a time. Files removed from the input folder do not
silently delete articles already stored. Collection schema mismatches fail closed.

Dense vectors come from the existing TEI server (`BAAI/bge-m3`, 1024 dimensions).
TEI 1.8's `/embed_sparse` requires SPLADE and does not expose bge-m3's lexical
head. The approved sparse sidecar uses the same bge-m3 revision's trained linear
head: ReLU over token hidden states, max positive weight per token ID, excluding
special tokens. It is not BM25 or a second model. See `infra/sparse/README.md`.
Queries remain in their original language. Both branches use Qdrant dense+sparse
RRF; cross-query fusion uses `1 / (60 + rank)` with deterministic chunk-ID ties.
The returned top six are chunks; scores are rank-fusion scores, not probabilities.

`RETRIEVAL__PARALLEL_TRANSLATE=false` is the default. When enabled, Mayura runs
concurrently with original-language search through the redacting Sarvam adapter.
The entire translated branch has a 250ms deadline; timeout/error falls back to
the original results. Timeout cancellation cannot guarantee that upstream work
or billing stopped. At most four translated tasks per retriever may be outstanding;
stuck cancelled work holds its slot, and further optional passes report `busy`
without making vendor calls. Reports estimate attempted translation spend separately from
vendor-confirmed adapter usage. Self-hosted embedding/vector API charges are zero;
the pricing table does not include machine operating costs.

No agent decision, answer generation or ticket implementation is provided by P2.
