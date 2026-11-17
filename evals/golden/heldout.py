"""Model and loader of the held-out retrieval questions (`evals/golden/heldout_questions.yaml`).

Kept apart from `schema.py` and `reference.py` on purpose: the tuning code imports those two,
and a test checks that `evals.tune` cannot reach this module, directly or through another import
(WP-14C, docs/EVALS.md section 3.2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from makna.model.definitions import Name

HELDOUT_QUESTIONS_FILE = Path(__file__).resolve().parent / "heldout_questions.yaml"
HELDOUT_ID_PATTERN = r"^H\d{2,}$"

HeldoutLanguage = Literal["en", "id"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class HeldoutQuestion(_Strict):
    """A question whose right answer is one metric, measured by search alone."""

    id: Annotated[str, StringConstraints(pattern=HELDOUT_ID_PATTERN)]
    language: HeldoutLanguage
    question: Annotated[str, StringConstraints(min_length=5)]
    expected_metric: Name


class HeldoutFile(_Strict):
    provenance: Annotated[str, StringConstraints(min_length=40)]
    """Who wrote the questions and how; the file does not load without it."""
    questions: list[HeldoutQuestion]


def load_heldout_questions(path: Path = HELDOUT_QUESTIONS_FILE) -> HeldoutFile:
    """Read and validate the file. Raises ValueError listing every problem."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        found = HeldoutFile.model_validate(raw)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in detail['loc'])}: {detail['msg']}"
            for detail in error.errors()
        ]
        raise ValueError(f"{path}: " + "; ".join(problems)) from None
    ids = [q.id for q in found.questions]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"{path}: ids used twice: {', '.join(duplicates)}")
    return found
