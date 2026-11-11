"""The hybrid search statement (ARCHITECTURE 4.1 and 8.2).

One SQL statement ranks the visible documents twice, by full-text score and by vector distance,
and fuses the two ranks:

    score = (w_keyword / (60 + keyword_rank) + w_vector / (60 + vector_rank))
            * status_weight * kind_weight

Which documents are visible depends on the caller's role (SRC-3): agents and viewers see
approved documents; curators, approvers, and admins also see drafts and proposals in review;
deprecated documents appear only when the caller asks. A catalog column is shown to viewers and
agents only when its catalog row exists and its derived sensitivity is not `personal` (a missing
row means hidden); curators and above see all, so search is not a map of where raw personal data
lives. Permissions beyond this filter belong to the service layer (WP-12).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, text

from makna.model.search import DOCUMENT_KINDS, EmbeddingProvider, SearchSettings
from makna.search import confidence as confidence_module
from makna.search.confidence import Confidence, Vocabulary, build_vocabulary, matched_terms
from makna.search.query import process

ROLES: tuple[str, ...] = ("viewer", "agent", "curator", "approver", "admin")
CURATOR_ROLES = frozenset({"curator", "approver", "admin"})
DEFAULT_KINDS = tuple(kind for kind in DOCUMENT_KINDS if kind != "column")
"""Without `kinds` a search looks at everything except raw catalog columns (section 12)."""
RRF_K = 60

SEARCH_SQL = text(
    """
WITH q AS (
  SELECT CASE WHEN CAST(:tsq AS text) IS NULL THEN NULL
              ELSE to_tsquery('simple', CAST(:tsq AS text)) END AS tsq,
         CAST(:vec AS vector) AS vec
),
cand AS (
  SELECT d.*
  FROM search_document d
  WHERE d.status = ANY(:statuses)
    AND d.kind = ANY(:kinds)
    AND (CAST(:domain AS text) IS NULL OR d.domain = CAST(:domain AS text))
    AND (NOT :hide_personal OR d.kind <> 'column' OR EXISTS (
          SELECT 1
          FROM catalog_column cc JOIN catalog_dataset ds ON ds.id = cc.dataset_id
          WHERE cc.derived_sensitivity IS DISTINCT FROM 'personal'
            AND ds.removed_at IS NULL
            AND d.object_id = 'column:' || ds.engine_fqn || '.' || cc.name))
),
kw AS (
  SELECT c.id, ts_rank_cd(c.tsv, q.tsq) AS score,
         row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC, c.id) AS rnk
  FROM cand c CROSS JOIN q
  WHERE q.tsq IS NOT NULL AND c.tsv @@ q.tsq
  ORDER BY score DESC, c.id
  LIMIT :candidates
),
vs AS (
  SELECT c.id, 1 - (c.embedding <=> q.vec) AS cosine,
         row_number() OVER (ORDER BY c.embedding <=> q.vec, c.id) AS rnk
  FROM cand c CROSS JOIN q
  ORDER BY c.embedding <=> q.vec, c.id
  LIMIT :candidates
),
fused AS (
  SELECT c.object_id, c.kind, c.status, c.domain, c.names, c.description,
         kw.score AS keyword_score, kw.rnk AS keyword_rank,
         vs.cosine AS vector_cosine, vs.rnk AS vector_rank,
         (COALESCE(CAST(:w_keyword AS double precision) / (60.0 + kw.rnk), 0)
          + COALESCE(CAST(:w_vector AS double precision) / (60.0 + vs.rnk), 0))
         * COALESCE(CAST(CAST(:status_weights AS jsonb) ->> c.status AS double precision), 0)
         * COALESCE(CAST(CAST(:kind_weights AS jsonb) ->> c.kind AS double precision), 0) AS score
  FROM cand c
  LEFT JOIN kw ON kw.id = c.id
  LEFT JOIN vs ON vs.id = c.id
  WHERE kw.id IS NOT NULL OR vs.id IS NOT NULL
)
SELECT * FROM fused
WHERE score > 0
ORDER BY score DESC, object_id
LIMIT :limit
"""
)

STATE_SQL = text(
    """
SELECT (SELECT count(*) FROM search_document) AS documents,
       (SELECT md5(COALESCE(string_agg(object_id || text_hash, ',' ORDER BY object_id), ''))
        FROM search_document) AS digest,
       (SELECT value FROM index_state WHERE key = 'embedding_model') AS model
"""
)


class SearchError(Exception):
    """The search cannot run; the message says what to do."""


@dataclass(frozen=True)
class Hit:
    object_id: str
    kind: str
    status: str
    domain: str | None
    names: str
    description: str
    score: float
    keyword_score: float | None
    keyword_rank: int | None
    vector_similarity: float | None
    vector_rank: int | None
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class SearchResult:
    query: str
    hits: tuple[Hit, ...]
    confidence: Confidence

    @property
    def ids(self) -> list[str]:
        return [hit.object_id for hit in self.hits]


def visible_statuses(role: str, include_deprecated: bool) -> list[str]:
    if role not in ROLES:
        raise SearchError(f"unknown role {role!r}; use one of {', '.join(ROLES)}")
    statuses = ["approved"]
    if role in CURATOR_ROLES:
        statuses += ["draft", "in_review"]
    if include_deprecated:
        statuses.append("deprecated")
    return statuses


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in vector) + "]"


class HybridSearch:
    """Searches the index of one database with one embedding provider."""

    def __init__(
        self, engine: Engine, provider: EmbeddingProvider, settings: SearchSettings | None = None
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._settings = settings or SearchSettings()
        self._vocabulary: tuple[tuple[object, ...], Vocabulary] | None = None

    def _vocabulary_for(self, connection: Connection) -> Vocabulary:
        """The word statistics, rebuilt only when the indexed documents changed."""
        state = connection.execute(STATE_SQL).one()
        if state.model is None or state.documents == 0:
            raise SearchError("the search index is empty; run `makna index build`")
        if state.model != self._provider.model_id:
            raise SearchError(
                f"the index was built with {state.model!r} but the configured provider is "
                f"{self._provider.model_id!r}; run `makna index build` to embed it again"
            )
        key = (state.documents, state.digest)
        if self._vocabulary is None or self._vocabulary[0] != key:
            rows = connection.execute(
                text(
                    "SELECT kind, names, CASE WHEN kind = 'dimension' THEN extra ELSE '' END "
                    "FROM search_document"
                )
            )
            self._vocabulary = (key, build_vocabulary((r[0], r[1], r[2]) for r in rows))
        return self._vocabulary[1]

    def search(
        self,
        query: str,
        *,
        role: str,
        kinds: list[str] | None = None,
        domain: str | None = None,
        include_deprecated: bool = False,
        limit: int = 5,
    ) -> SearchResult:
        statuses = visible_statuses(role, include_deprecated)
        chosen = list(kinds) if kinds else list(DEFAULT_KINDS)
        unknown = sorted(set(chosen) - set(DOCUMENT_KINDS))
        if unknown:
            raise SearchError(
                f"unknown kind(s) {', '.join(unknown)}; use {', '.join(DOCUMENT_KINDS)}"
            )
        processed = process(query)
        vector = vector_literal(self._provider.embed_query(query))
        settings = self._settings
        with self._engine.connect() as connection:
            vocabulary = self._vocabulary_for(connection)
            rows = connection.execute(
                SEARCH_SQL,
                {
                    "tsq": processed.tsquery,
                    "vec": vector,
                    "statuses": statuses,
                    "kinds": chosen,
                    "domain": domain,
                    "hide_personal": role not in CURATOR_ROLES,
                    "candidates": settings.candidates,
                    "w_keyword": settings.w_keyword,
                    "w_vector": settings.w_vector,
                    "status_weights": json.dumps(settings.status_weights),
                    "kind_weights": json.dumps(settings.kind_weights),
                    "limit": max(limit, confidence_module.TOP_HITS),
                },
            ).all()
        hits = tuple(
            Hit(
                object_id=r.object_id,
                kind=r.kind,
                status=r.status,
                domain=r.domain,
                names=r.names,
                description=r.description,
                score=float(r.score),
                keyword_score=None if r.keyword_score is None else float(r.keyword_score),
                keyword_rank=r.keyword_rank,
                vector_similarity=None if r.vector_cosine is None else float(r.vector_cosine),
                vector_rank=r.vector_rank,
                matched_terms=tuple(matched_terms(processed.words, r.names)),
            )
            for r in rows
        )
        band = confidence_module.assess(
            processed.words,
            [h.names for h in hits],
            [h.vector_similarity for h in hits],
            vocabulary,
            settings.confidence,
        )
        return SearchResult(query, hits[:limit], band)
