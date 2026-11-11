"""Embedding providers (ARCHITECTURE 8.1, SRC-4).

| provider            | use                                                                  |
|---------------------|----------------------------------------------------------------------|
| `fastembed`         | default: a local ONNX model inside the process, no network at run time |
| `ollama`            | a local Ollama server                                                |
| `openai_compatible` | an endpoint with the OpenAI embeddings API shape                     |
| `hash`              | a deterministic stand-in for tests; not for real use                 |

Every provider returns plain lists of floats, so the callers never depend on numpy.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from makna.model.search import (
    EmbeddingProvider,
    EmbeddingSettings,
    FastembedSettings,
    OllamaSettings,
    OpenAICompatibleSettings,
)
from makna.search.query import content_candidates

__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "FastembedProvider",
    "HashProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "make_provider",
]

BATCH_SIZE = 64
REQUEST_TIMEOUT = 60.0


class EmbeddingError(Exception):
    """A provider could not produce vectors; the message says what to check."""


def _batches(texts: Sequence[str], size: int = BATCH_SIZE) -> list[list[str]]:
    return [list(texts[i : i + size]) for i in range(0, len(texts), size)]


class HashProvider:
    """Hashed character trigrams of the non-stop words, L2-normalised.

    It tolerates typos and shares vocabulary, but it does not understand meaning. It exists so
    that tests and CI run the whole search path without a model.
    """

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = dimension
        self.model_id = f"hash-trigram-{dimension}"

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in content_candidates(text):
            padded = f"  {word} "
            for i in range(len(padded) - 2):
                digest = hashlib.blake2b(padded[i : i + 3].encode(), digest_size=4).digest()
                vector[int.from_bytes(digest, "little") % self.dimension] += 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class FastembedProvider:
    """The default: a multilingual model run with ONNX on the CPU, inside this process.

    Unless `allow_download` is set, only files already in `cache_dir` are used and the network
    is never tried (NFR-3): the Docker image downloads the model once, at build time.
    """

    def __init__(self, settings: FastembedSettings | None = None) -> None:
        self._settings = settings or FastembedSettings()
        self.model_id = self._settings.model
        self._model: Any = None
        self._dimension: int | None = None

    def _load(self) -> Any:
        if self._model is None:
            from fastembed import TextEmbedding

            try:
                self._model = TextEmbedding(
                    model_name=self._settings.model,
                    cache_dir=self._settings.cache_dir,
                    local_files_only=not self._settings.allow_download,
                )
            except Exception as error:
                raise EmbeddingError(
                    f"cannot load the embedding model {self._settings.model!r} from "
                    f"{self._settings.cache_dir or 'the default cache'}: {error}. The image "
                    "downloads it at build time; outside Docker set "
                    "embedding.fastembed.allow_download or choose another provider."
                ) from error
        return self._model

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            from fastembed import TextEmbedding

            try:
                self._dimension = int(TextEmbedding.get_embedding_size(self._settings.model))
            except ValueError as error:
                raise EmbeddingError(str(error)) from error
        return self._dimension

    def _embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return [[float(x) for x in vector] for vector in model.embed(texts)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class _HttpProvider:
    """Shared part of the two HTTP providers: one POST per batch, dimension from the answer."""

    model_id: str

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers or {},
            timeout=REQUEST_TIMEOUT,
            transport=transport,
        )
        self._model = model
        self.model_id = model
        self._dimension: int | None = None

    def _request(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = self._client.post(path, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise EmbeddingError(
                f"embedding request to {self._client.base_url}{path} failed: {error}"
            ) from error
        except ValueError as error:
            raise EmbeddingError(f"embedding answer is not JSON: {error}") from error

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batches(texts):
            found = self._request(batch)
            if len(found) != len(batch):
                raise EmbeddingError(f"asked for {len(batch)} vectors, got {len(found)}")
            vectors.extend(found)
        sizes = {len(v) for v in vectors}
        if len(sizes) > 1 or (self._dimension is not None and sizes - {self._dimension}):
            raise EmbeddingError(
                f"vector sizes differ: {sorted(sizes)} (expected {self._dimension})"
            )
        if sizes and self._dimension is None:
            self._dimension = sizes.pop()
        return vectors

    @property
    def dimension(self) -> int:
        """Known after the first answer; the first read asks for one vector to learn it."""
        if self._dimension is None:
            self._embed(["dimension probe"])
        assert self._dimension is not None
        return self._dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


class OllamaProvider(_HttpProvider):
    """A local Ollama server: `POST /api/embed` with `{model, input: [...]}`."""

    def __init__(
        self,
        settings: OllamaSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        chosen = settings or OllamaSettings()
        super().__init__(chosen.url, chosen.model, transport=transport)

    def _request(self, texts: list[str]) -> list[list[float]]:
        answer = self._post("api/embed", {"model": self._model, "input": texts})
        try:
            return [[float(x) for x in vector] for vector in answer["embeddings"]]
        except (KeyError, TypeError) as error:
            raise EmbeddingError(f"unexpected answer from Ollama: {error!r}") from error


class OpenAICompatibleProvider(_HttpProvider):
    """An endpoint with the OpenAI embeddings shape: `POST {url}/embeddings`."""

    def __init__(
        self,
        settings: OpenAICompatibleSettings | None = None,
        api_key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        chosen = settings or OpenAICompatibleSettings()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        super().__init__(chosen.url, chosen.model, headers=headers, transport=transport)

    def _request(self, texts: list[str]) -> list[list[float]]:
        answer = self._post("embeddings", {"model": self._model, "input": texts})
        try:
            rows = sorted(answer["data"], key=lambda row: row["index"])
            return [[float(x) for x in row["embedding"]] for row in rows]
        except (KeyError, TypeError) as error:
            raise EmbeddingError(f"unexpected answer from the endpoint: {error!r}") from error


_FACTORIES: dict[str, Callable[[EmbeddingSettings, str | None], EmbeddingProvider]] = {
    "fastembed": lambda s, _key: FastembedProvider(s.fastembed),
    "ollama": lambda s, _key: OllamaProvider(s.ollama),
    "openai_compatible": lambda s, key: OpenAICompatibleProvider(s.openai_compatible, key),
    "hash": lambda s, _key: HashProvider(s.hash.dimension),
}


def make_provider(
    name: str, settings: EmbeddingSettings | None = None, api_key: str | None = None
) -> EmbeddingProvider:
    """The provider called `name`. The API key (openai_compatible) comes from the environment."""
    factory = _FACTORIES.get(name)
    if factory is None:
        raise EmbeddingError(
            f"unknown embedding provider {name!r}; use one of {sorted(_FACTORIES)}"
        )
    return factory(settings or EmbeddingSettings(), api_key)
