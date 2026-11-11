"""WP-07 acceptance 5: every provider passes one contract test; no test downloads a model.

`ollama` and `openai_compatible` run against a fake HTTP server on the loopback interface.
`fastembed` runs against a stand-in for the model class (the real model is checked in the CI
stack job, `deploy/check_search.py`).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
import pytest

from makna.model.search import (
    EmbeddingProvider,
    FastembedSettings,
    OllamaSettings,
    OpenAICompatibleSettings,
)
from makna.search.embedding import (
    EmbeddingError,
    FastembedProvider,
    HashProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    make_provider,
)

DIMENSION = 12


def fake_vector(text: str, dimension: int = DIMENSION) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255 for i in range(dimension)]


class FakeEmbeddingServer(ThreadingHTTPServer):
    """Answers the Ollama and the OpenAI embeddings calls, and records what it was asked."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.wrong_count = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeEmbeddingServer

    def log_message(self, format: str, *args: Any) -> None:  # silence the test output
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        )
        if self.server.status != 200:
            self.send_response(self.server.status)
            self.end_headers()
            return
        texts = body["input"][:-1] if self.server.wrong_count else body["input"]
        vectors = [fake_vector(t) for t in texts]
        if self.path == "/api/embed":
            answer: dict[str, Any] = {"model": body["model"], "embeddings": vectors}
        elif self.path == "/v1/embeddings":
            # a real endpoint may answer in any order; `index` says which input is which
            rows = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
            answer = {"object": "list", "data": list(reversed(rows))}
        else:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server() -> Iterator[FakeEmbeddingServer]:
    fake = FakeEmbeddingServer()
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    yield fake
    fake.shutdown()
    fake.server_close()


class FakeTextEmbedding:
    """Stands in for `fastembed.TextEmbedding`: same calls, no model, no network."""

    instances: list[FakeTextEmbedding] = []

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        self.model_name = model_name
        self.kwargs = kwargs
        FakeTextEmbedding.instances.append(self)

    @classmethod
    def get_embedding_size(cls, model_name: str) -> int:
        if model_name != "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2":
            raise ValueError(f"Embedding size for model {model_name} was None")
        return 384

    def embed(self, texts: list[str], **_: Any) -> Iterator[Any]:
        for text in texts:
            yield np.array(fake_vector(text, 384), dtype=np.float32)


@pytest.fixture
def fastembed_stub(monkeypatch: pytest.MonkeyPatch) -> type[FakeTextEmbedding]:
    import fastembed

    FakeTextEmbedding.instances = []
    monkeypatch.setattr(fastembed, "TextEmbedding", FakeTextEmbedding)
    return FakeTextEmbedding


ProviderFactory = Callable[[FakeEmbeddingServer], EmbeddingProvider]
FACTORIES: dict[str, ProviderFactory] = {
    "hash": lambda _s: HashProvider(64),
    "fastembed": lambda _s: FastembedProvider(FastembedSettings(cache_dir="/models")),
    "ollama": lambda s: OllamaProvider(OllamaSettings(url=s.url, model="fake-embed")),
    "openai_compatible": lambda s: OpenAICompatibleProvider(
        OpenAICompatibleSettings(url=f"{s.url}/v1", model="fake-embed"), api_key="k-123"
    ),
}


@pytest.mark.parametrize("name", FACTORIES)
def test_provider_contract(
    name: str, server: FakeEmbeddingServer, fastembed_stub: type[FakeTextEmbedding]
) -> None:
    provider = FACTORIES[name](server)
    text = "Berapa rasio kredit macet bulan lalu?"
    first, second = provider.embed_query(text), provider.embed_query(text)
    assert len(first) == provider.dimension  # the dimension matches
    assert first == second  # the same text gives the same vector
    assert provider.embed_documents([text, "NPF"])[0] == pytest.approx(first, abs=1e-6)
    assert [len(v) for v in provider.embed_documents(["a", "b", "c"])] == [provider.dimension] * 3
    assert provider.embed_documents([]) == []
    assert provider.model_id


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_the_hash_provider_is_normalised_and_tolerates_typos() -> None:
    provider = HashProvider()
    vector = provider.embed_query("disbursement amount")
    assert sum(x * x for x in vector) == pytest.approx(1.0)
    typo = provider.embed_query("disbursment amout")
    other = provider.embed_query("collection rate")
    assert dot(vector, typo) > dot(vector, other)
    assert provider.embed_query("") == [0.0] * provider.dimension


def test_hash_provider_dimension_is_configurable() -> None:
    assert len(HashProvider(32).embed_query("npf")) == 32
    assert HashProvider(32).model_id != HashProvider(64).model_id


def test_fastembed_uses_only_local_files_by_default(
    fastembed_stub: type[FakeTextEmbedding],
) -> None:
    provider = FastembedProvider(FastembedSettings(cache_dir="/opt/makna/models"))
    assert provider.dimension == 384  # known without loading the model
    assert fastembed_stub.instances == []
    provider.embed_query("npf")
    (model,) = fastembed_stub.instances
    assert model.kwargs["local_files_only"] is True
    assert model.kwargs["cache_dir"] == "/opt/makna/models"
    assert model.model_name == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def test_fastembed_loads_the_model_once(fastembed_stub: type[FakeTextEmbedding]) -> None:
    provider = FastembedProvider()
    provider.embed_query("a")
    provider.embed_documents(["b", "c"])
    assert len(fastembed_stub.instances) == 1


def test_fastembed_downloads_only_when_asked(fastembed_stub: type[FakeTextEmbedding]) -> None:
    FastembedProvider(FastembedSettings(allow_download=True)).embed_query("npf")
    assert fastembed_stub.instances[0].kwargs["local_files_only"] is False


def test_fastembed_explains_a_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed

    class Missing:
        def __init__(self, **_: Any) -> None:
            raise ValueError("Could not find the model files and local_files_only=True")

    monkeypatch.setattr(fastembed, "TextEmbedding", Missing)
    with pytest.raises(EmbeddingError, match="downloads it at build time"):
        FastembedProvider(FastembedSettings(cache_dir="/nowhere")).embed_query("npf")


def test_ollama_request_shape(server: FakeEmbeddingServer) -> None:
    OllamaProvider(OllamaSettings(url=server.url + "/", model="m1")).embed_documents(["x", "y"])
    assert server.requests == [
        {"path": "/api/embed", "body": {"model": "m1", "input": ["x", "y"]}, "auth": None}
    ]


def test_openai_request_shape_and_key(server: FakeEmbeddingServer) -> None:
    provider = OpenAICompatibleProvider(
        OpenAICompatibleSettings(url=server.url + "/v1", model="m2"), api_key="secret-key"
    )
    vectors = provider.embed_documents(["x", "y"])
    assert vectors == [pytest.approx(fake_vector("x")), pytest.approx(fake_vector("y"))]
    (request,) = server.requests
    assert request["path"] == "/v1/embeddings"
    assert request["body"] == {"model": "m2", "input": ["x", "y"]}
    assert request["auth"] == "Bearer secret-key"


def test_a_large_batch_is_split(server: FakeEmbeddingServer) -> None:
    provider = OllamaProvider(OllamaSettings(url=server.url, model="m"))
    texts = [f"text {i}" for i in range(150)]
    assert len(provider.embed_documents(texts)) == 150
    assert [len(r["body"]["input"]) for r in server.requests] == [64, 64, 22]


def test_dimension_is_learned_from_the_first_answer(server: FakeEmbeddingServer) -> None:
    provider = OllamaProvider(OllamaSettings(url=server.url, model="m"))
    assert server.requests == []
    assert provider.dimension == DIMENSION
    assert len(server.requests) == 1


@pytest.mark.parametrize("factory", ["ollama", "openai_compatible"])
def test_a_server_error_is_an_embedding_error(factory: str, server: FakeEmbeddingServer) -> None:
    server.status = 503
    with pytest.raises(EmbeddingError, match="503"):
        FACTORIES[factory](server).embed_query("npf")


def test_a_short_answer_is_an_embedding_error(server: FakeEmbeddingServer) -> None:
    server.wrong_count = True
    with pytest.raises(EmbeddingError, match="asked for 2 vectors, got 1"):
        FACTORIES["ollama"](server).embed_documents(["a", "b"])


def test_an_unreachable_server_is_an_embedding_error() -> None:
    provider = OllamaProvider(OllamaSettings(url="http://127.0.0.1:9", model="m"))
    with pytest.raises(EmbeddingError, match="failed"):
        provider.embed_query("npf")


def test_make_provider_picks_by_name_and_passes_the_key(server: FakeEmbeddingServer) -> None:
    assert isinstance(make_provider("hash"), HashProvider)
    assert isinstance(make_provider("fastembed"), FastembedProvider)
    assert isinstance(make_provider("ollama"), OllamaProvider)
    provider = make_provider("openai_compatible", api_key="abc")
    assert isinstance(provider, OpenAICompatibleProvider)
    with pytest.raises(EmbeddingError, match="unknown embedding provider"):
        make_provider("telepathy")
