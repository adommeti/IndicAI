import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from indic_platform.adapters.embeddings import Embedding, TEIEmbedder
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.obs.langfuse import MemorySink


def test_bge_lexical_weights_max_pool_and_exclusions() -> None:
    path = Path(__file__).parents[2] / "infra/sparse/server.py"
    spec = importlib.util.spec_from_file_location("sparse_server_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert module.lexical_weights([0, 7, 7, 8, 9, 2], [99, 0.2, 0.7, 0, -0.1, 99], {0, 2}) == {
            7: 0.7
        }
    finally:
        sys.modules.pop(spec.name, None)


async def test_dense_sparse_http_contract_and_redaction() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert "person@example.com" not in str(body)
        if request.url.path == "/embed":
            assert body == {"inputs": "Help [EMAIL]", "truncate": False}
            return httpx.Response(200, json=[[1.0] + [0.0] * 1023])
        assert body == {"inputs": ["Help [EMAIL]"]}
        return httpx.Response(200, json=[{"7": 0.5}])

    sink = MemorySink()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    embedder = TEIEmbedder(
        "http://tei",
        "http://sparse",
        client=client,
        runtime=AdapterRuntime("tei", "embedding", sink=sink),
    )
    result = await embedder.embed(["Help person@example.com"])
    assert result[0].sparse == {7: 0.5}
    assert len(requests) == 2
    requests.clear()
    assert (await embedder.embed(["Help person@example.com"], sparse=False))[0].sparse == {}
    assert len(requests) == 1
    assert sink.records[0]["model"] == "BAAI/bge-m3"
    assert sink.records[0]["cost_inr"] == 0
    await embedder.close()


@pytest.mark.parametrize(
    "dense,sparse",
    [
        ([0.0], {7: 1}),
        ([float("nan")] * 1024, {}),
        ([0.0] * 1024, {-1: 1}),
        ([0.0] * 1024, {7: -1}),
    ],
)
def test_malformed_embeddings_rejected(dense: list[float], sparse: dict[int, float]) -> None:
    with pytest.raises(ValueError):
        Embedding(dense=dense, sparse=sparse)


async def test_wrong_model_fails_preflight() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"model_id": "wrong"}))
    )
    embedder = TEIEmbedder("http://tei", "http://sparse", client=client)
    with pytest.raises(ValueError, match="bge-m3"):
        await embedder.check()
    await embedder.close()
