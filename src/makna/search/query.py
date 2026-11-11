"""Query processing (ARCHITECTURE 4.1 step 2): words, stop words, and the full-text query.

Lower-case the text, split it into words, drop English and Indonesian stop words, and OR the
remaining words together. A word of four letters or more matches as a prefix, so "disburse"
finds "disbursement". The `simple` text search configuration does no stemming, which suits mixed
Indonesian and English.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOP_WORDS: frozenset[str] = frozenset(
    """
    a an the of is are was were be been being what which who whom whose how when where why
    do does did we our us you your i me my it its in on at by for to from and or but not
    show give tell please this that these those there here with without about as into
    per much many some any all each every
    total number amount list get find see have has had can could should would will
    apa berapa yang di ke dari dan atau tetapi untuk dengan pada kita kami saya anda kamu
    tolong tampilkan lihat jumlah ini itu adalah ialah bagaimana mana siapa kapan mengapa
    kenapa nya tiap setiap semua seluruh masing para oleh dalam antara bagi sebagai
    sudah telah akan sedang bisa dapat ada
    """.split()
)
"""Words that carry no meaning for finding a definition. Not time words: those are in
`makna.search.terms`, because they are qualifiers, not noise."""

PREFIX_MIN_LENGTH = 4
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words(text: str) -> list[str]:
    """Lower-case words of `text` (letters and digits), in order, with repeats removed."""
    seen: dict[str, None] = {}
    for found in _WORD.findall(text.lower()):
        seen.setdefault(found)
    return list(seen)


def content_candidates(text: str) -> list[str]:
    """The words of `text` that are not stop words, and not single characters."""
    return [w for w in words(text) if w not in STOP_WORDS and len(w) > 1]


@dataclass(frozen=True)
class ProcessedQuery:
    text: str
    words: tuple[str, ...]
    """Non-stop words, in the order they appear."""

    @property
    def tsquery(self) -> str | None:
        """The text for `to_tsquery('simple', ...)`: words ORed, prefix match from 4 letters.

        None when no word is left, so the keyword list is empty.
        """
        if not self.words:
            return None
        return " | ".join(
            f"'{w}':*" if len(w) >= PREFIX_MIN_LENGTH else f"'{w}'" for w in self.words
        )


def process(text: str) -> ProcessedQuery:
    """Split a query into its non-stop words."""
    return ProcessedQuery(text, tuple(content_candidates(text)))


def word_matches(query_word: str, name_token: str) -> bool:
    """A query word hits a name token when equal, or a prefix of it from four letters."""
    if query_word == name_token:
        return True
    return len(query_word) >= PREFIX_MIN_LENGTH and name_token.startswith(query_word)
