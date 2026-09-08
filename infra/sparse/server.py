"""BGE-M3's learned lexical head; TEI 1.8 serves only the dense CLS head.

Implements FlagEmbedding's ReLU(sparse_linear(last_hidden_state)), taking the
maximum positive weight per token ID and excluding CLS/EOS/PAD/UNK. This is not
SPLADE or BM25. Model code is never downloaded/executed (trust_remote_code=False).
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    import torch
    from huggingface_hub import hf_hub_download
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(int(os.getenv("SPARSE_THREADS", "2")))
    tokenizer = AutoTokenizer.from_pretrained(MODEL, revision=REVISION, trust_remote_code=False)
    model = AutoModel.from_pretrained(
        MODEL, revision=REVISION, trust_remote_code=False, torch_dtype=torch.float16
    ).eval()
    head = torch.nn.Linear(model.config.hidden_size, 1)
    head.load_state_dict(
        torch.load(
            hf_hub_download(MODEL, "sparse_linear.pt", revision=REVISION),
            map_location="cpu",
            weights_only=True,
        )
    )
    state.update(torch=torch, tokenizer=tokenizer, model=model, head=head.half().eval())
    yield
    state.clear()


app = FastAPI(lifespan=lifespan)


class Input(BaseModel):
    inputs: list[str] = Field(min_length=1, max_length=1)


@app.get("/info")
def info() -> dict[str, str]:
    return {
        "model_id": MODEL,
        "model_sha": REVISION,
        "pooling": "bge-m3-lexical",
        "dtype": "float16",
    }


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ready": bool(state)}


def lexical_weights(ids: list[int], weights: list[float], excluded: set[int]) -> dict[int, float]:
    sparse: dict[int, float] = {}
    for id, weight in zip(ids, weights, strict=True):
        if id not in excluded and weight > 0:
            sparse[id] = max(sparse.get(id, 0), weight)
    return sparse


@app.post("/embed_sparse")
def embed(request: Input) -> list[dict[int, float]]:
    tokenizer, torch = state["tokenizer"], state["torch"]
    inputs = tokenizer(request.inputs, padding=True, truncation=False, return_tensors="pt")
    if inputs["input_ids"].shape[1] > 512:
        raise HTTPException(413, "Maximum input length is 512 tokens; truncation is disabled")
    with torch.inference_mode():
        hidden = state["model"](**inputs).last_hidden_state
        weights = torch.relu(state["head"](hidden)).squeeze(-1).float().tolist()
    excluded = {
        tokenizer.cls_token_id,
        tokenizer.eos_token_id,
        tokenizer.pad_token_id,
        tokenizer.unk_token_id,
    }
    result = []
    for ids, values in zip(inputs["input_ids"].tolist(), weights, strict=True):
        result.append(lexical_weights(ids, values, excluded))
    return result
