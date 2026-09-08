# BGE-M3 sparse service

Use `docker compose --env-file .env.stack up -d --build bge-sparse`.
The optional `retrieval` profile keeps this memory-heavy service out of the
default P0 stack startup; explicitly naming the service activates it.
It shares the TEI model cache, pins model revision
`5617a9f61b028005a4858fdac845db406aefb181`, uses float16 CPU inference, and
binds only localhost port 8081. TEI continues to serve dense vectors at 8080.
Model loading disables remote code; the lexical head is loaded with
`torch.load(weights_only=True)`. Inputs over 512 tokens are rejected, not truncated.

The full stack plus two encoder processes can exceed Docker Desktop's 8GB VM.
On this development Mac the sparse container was OOM-killed at startup. Without
changing Docker's global allocation, run the same service natively instead:

```sh
HF_HUB_DISABLE_XET=1 uv run --isolated --no-project --python 3.12 \
  --with-requirements infra/sparse/requirements.txt \
  python -m uvicorn infra.sparse.server:app \
  --host 127.0.0.1 --port 8081 --limit-concurrency 4
```

Keep the native process running for ingestion/eval. Do not start native and Docker
instances on the same port. The isolated environment avoids mixing the platform's
tokenizer dependency with the model-serving dependencies. Downloads use the
Hugging Face cache; no weights are committed to git. Initial download is several GB.

Algorithm reference: FlagEmbedding's BGE-M3 `sparse_embedding` and
`_process_token_weights` implementations in its encoder-only M3 model. Sparse
weights are learned lexical weights, with no language translation or stemming.
