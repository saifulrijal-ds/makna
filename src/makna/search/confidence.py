"""The confidence band of a result set (ARCHITECTURE 8.3).

Rank scores cannot say "nothing matches" (`docs/notes/search-scores-cannot-say-no-match.md`), so
the search returns evidence: how much of the query's informative vocabulary appears in the names
and synonyms of the top hits, and a band made from it. The final decision to abstain belongs to
the caller, who reads the descriptions.

The query's words are first split. **Qualifier words** describe a cut or a period instead of
naming a metric: the time and grain vocabulary (`makna.search.terms`), and words that occur in
the names, synonyms, or declared values of an indexed dimension but in the name of no metric or
glossary term. The rest are **content words**. Coverage is the share, weighted by inverse
document frequency over the `names` field, of the content words found in the `names` of any of
the top 3 hits.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from makna.model.search import ConfidenceSettings
from makna.search.query import word_matches
from makna.search.terms import is_time_word

Band = Literal["high", "medium", "low"]
TOP_HITS = 3
LOW_GUIDANCE = (
    "No governed definition clearly matches. Tell the user that no approved metric exists for "
    "this; do not approximate it with a different metric."
)


@dataclass(frozen=True)
class Vocabulary:
    """Word statistics of the indexed documents; built once per index state."""

    n_documents: int = 0
    token_documents: dict[str, int] = field(default_factory=dict)
    """Name token -> number of documents whose `names` contain it."""
    metric_tokens: frozenset[str] = frozenset()
    """Tokens of the names of metric and glossary term documents."""
    dimension_tokens: frozenset[str] = frozenset()
    """Tokens of the names, synonyms, and declared values of dimension documents."""

    def document_frequency(self, word: str) -> int:
        """Documents whose names hold the word (a prefix match from four letters)."""
        found = sum(
            count for token, count in self.token_documents.items() if word_matches(word, token)
        )
        return min(found, self.n_documents)

    def idf(self, word: str) -> float:
        return math.log((self.n_documents + 1) / (self.document_frequency(word) + 0.5))


def tokens_of(text: str) -> set[str]:
    from makna.search.query import words

    return set(words(text))


def build_vocabulary(documents: Iterable[tuple[str, str, str]]) -> Vocabulary:
    """From `(kind, names, extra)` of every document."""
    count = 0
    token_documents: dict[str, int] = {}
    metric: set[str] = set()
    dimension: set[str] = set()
    for kind, names, extra in documents:
        count += 1
        name_tokens = tokens_of(names)
        for token in name_tokens:
            token_documents[token] = token_documents.get(token, 0) + 1
        if kind in ("metric", "glossary_term"):
            metric |= name_tokens
        elif kind == "dimension":
            dimension |= name_tokens | tokens_of(extra)
    return Vocabulary(count, token_documents, frozenset(metric), frozenset(dimension))


def _in(word: str, tokens: Iterable[str]) -> bool:
    return any(word_matches(word, token) for token in tokens)


def split_words(query_words: Sequence[str], vocabulary: Vocabulary) -> tuple[list[str], list[str]]:
    """Content words and qualifier words of a query's non-stop words."""
    content: list[str] = []
    qualifiers: list[str] = []
    for word in query_words:
        if is_time_word(word):
            qualifiers.append(word)
        elif _in(word, vocabulary.dimension_tokens) and not _in(word, vocabulary.metric_tokens):
            qualifiers.append(word)
        else:
            content.append(word)
    return content, qualifiers


@dataclass(frozen=True)
class Confidence:
    band: Band
    coverage: float
    content_words: tuple[str, ...]
    qualifier_words: tuple[str, ...]
    top_cosine: float | None
    guidance: str | None = None


def matched_terms(query_words: Iterable[str], names: str) -> list[str]:
    """The query words that appear in one hit's names (equal, or a prefix from four letters)."""
    hit_tokens = tokens_of(names)
    return [word for word in query_words if _in(word, hit_tokens)]


def assess(
    query_words: Sequence[str],
    hit_names: Sequence[str],
    cosines: Sequence[float | None],
    vocabulary: Vocabulary,
    settings: ConfidenceSettings,
) -> Confidence:
    """The band of a result set. `hit_names` and `cosines` are those of the hits, best first."""
    content, qualifiers = split_words(query_words, vocabulary)
    known = [c for c in cosines if c is not None]
    top_cosine = max(known) if known else None
    top_tokens: set[str] = set()
    for names in hit_names[:TOP_HITS]:
        top_tokens |= tokens_of(names)
    total = sum(vocabulary.idf(word) for word in content)
    covered = sum(vocabulary.idf(word) for word in content if _in(word, top_tokens))
    coverage = covered / total if total > 0 else 0.0
    weak_vector = (
        settings.low_cosine is None or top_cosine is None or top_cosine < settings.low_cosine
    )
    if not content or (coverage < settings.low and weak_vector):
        return Confidence(
            "low", coverage, tuple(content), tuple(qualifiers), top_cosine, LOW_GUIDANCE
        )
    band: Band = "high" if coverage >= settings.high else "medium"
    return Confidence(band, coverage, tuple(content), tuple(qualifiers), top_cosine)
