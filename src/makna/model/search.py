"""Search and embedding vocabulary shared by the indexer and the search (ARCHITECTURE 8.1, 8.2).

Plain data and one protocol, no I/O. The indexer writes documents and embeds them with an
`EmbeddingProvider`; the search embeds the query with the same provider. The settings models
live here so that neither package has to import the other, or the configuration module.
"""

from __future__ import annotations

from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DOCUMENT_KINDS = ("metric", "entity", "dimension", "rule", "glossary_term", "domain", "column")


class EmbeddingProvider(Protocol):
    """Turns text into vectors. One instance embeds both documents and queries."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


Weight = Annotated[float, Field(ge=0)]


def _status_weights() -> dict[str, float]:
    return {"approved": 1.0, "deprecated": 0.5, "draft": 0.5, "in_review": 0.5}


def _kind_weights() -> dict[str, float]:
    return {
        "metric": 1.0,
        "glossary_term": 0.9,
        "entity": 0.8,
        "dimension": 0.8,
        "rule": 0.8,
        "domain": 0.7,
        "column": 0.6,
    }


class ConfidenceSettings(BaseModel):
    """Thresholds of the confidence band (section 8.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    high: Annotated[float, Field(ge=0, le=1)] = 0.6
    low: Annotated[float, Field(ge=0, le=1)] = 0.3
    low_cosine: float | None = None
    """Set from the real model's score distribution (WP-15); while unset only coverage is used."""


class SearchSettings(BaseModel):
    """The weights of the fusion statement (section 8.2); `[search]` in `config/makna.toml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    w_keyword: Weight = 1.0
    w_vector: Weight = 1.0
    status_weights: dict[str, Weight] = Field(default_factory=_status_weights)
    kind_weights: dict[str, Weight] = Field(default_factory=_kind_weights)
    candidates: Annotated[int, Field(ge=1)] = 30
    """How many documents each of the two ranked lists keeps."""
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)

    def status_weight(self, status: str) -> float:
        return self.status_weights.get(status, 0.0)

    def kind_weight(self, kind: str) -> float:
        return self.kind_weights.get(kind, 0.0)


class FastembedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = DEFAULT_EMBEDDING_MODEL
    cache_dir: str | None = None
    """Where the model files are. The image bakes the model into one fixed directory."""
    allow_download: bool = False
    """False: only files already in `cache_dir` are used, and the network is never tried."""


class OllamaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = "http://localhost:11434"
    model: str = "bge-m3"


class OpenAICompatibleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = "https://api.openai.com/v1"
    model: str = "text-embedding-3-small"


class HashSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: Annotated[int, Field(ge=8)] = 256


class EmbeddingSettings(BaseModel):
    """Non-secret settings of the providers; `[embedding.*]` in `config/makna.toml`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fastembed: FastembedSettings = Field(default_factory=FastembedSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    openai_compatible: OpenAICompatibleSettings = Field(default_factory=OpenAICompatibleSettings)
    hash: HashSettings = Field(default_factory=HashSettings)
