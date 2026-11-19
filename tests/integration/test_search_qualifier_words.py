"""WP-20 disclosure: the qualifier words added to `makna.search.terms` (ordinals, "terakhir",
"final", "sepanjang", "throughout", "over") change the confidence band of questions that use them,
and nothing else about a search (ARCHITECTURE 8.3, "Details settled in WP-20"). Two golden
questions, taken from the golden file by their text, pin the direction: medium before, high now."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from evals.golden.reference import QUESTIONS_FILE
from evals.golden.schema import load_questions
from makna.search import terms

from . import service_support as sup
from .service_support import Users

PINNED = (
    "Berapa jumlah persetujuan kredit pada kuartal pertama 2026?",
    "How much late-fee income did we book in the second quarter of 2026?",
)
ADDED = {"final", "sepanjang", "throughout", "over"}


def test_the_pinned_questions_are_in_the_golden_file() -> None:
    assert set(PINNED) <= {q.question for q in load_questions(QUESTIONS_FILE)}


@pytest.mark.parametrize("question", PINNED)
def test_an_ordinal_is_a_qualifier_so_the_band_is_high_and_not_medium(
    service_world: tuple[Engine, Users], monkeypatch: pytest.MonkeyPatch, question: str
) -> None:
    engine, users = service_world
    svc = sup.services(engine)
    now = svc.search(users.caller("viewer"), question)
    before = terms.QUALIFIER_WORDS - set(terms.ORDINALS) - ADDED
    monkeypatch.setattr(terms, "QUALIFIER_WORDS", before)
    then = svc.search(users.caller("viewer"), question)
    assert [h.id for h in now.hits] == [h.id for h in then.hits]  # the ranking is untouched
    assert (then.confidence, now.confidence) == ("medium", "high")
