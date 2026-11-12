"""Model of `evals/golden/questions.yaml` (docs/EVALS.md section 2.2).

The file is a YAML list of questions. `load_questions` reads it and raises `ValueError` with the
problems found. The model checks the shape (which fields each outcome needs and allows); the
unit tests check it against the definitions and the files beside it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self, get_args

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from makna.model.definitions import Name, RequestBody
from makna.model.request import QueryRequest

Language = Literal["en", "id", "mixed"]
Outcome = Literal["answer", "clarify", "no_metric", "refuse"]
Group = Literal[
    "direct_english",
    "direct_indonesian_or_mixed",
    "dimensions_and_filters",
    "time_handling",
    "governed_default",
    "clarify",
    "no_metric",
    "refuse",
]
Reason = Literal["no_definition", "dimension_not_available", "not_approved", "personal_data"]
GROUPS: tuple[str, ...] = get_args(Group)
"""The eight rows of docs/EVALS.md table 2.4, in table order."""

ID_PATTERN = r"^G\d{2,}$"
TAG_PATTERN = r"^[a-z][a-z0-9_]*$"
COLUMN_PATTERN = r"^[a-z][a-z0-9_]*$"

ERROR_CODE_FOR_REASON = {
    "dimension_not_available": "DIMENSION_NOT_AVAILABLE",
    "not_approved": "NOT_APPROVED",
}
"""The planner error code a `no_metric` request must fail with (docs/EVALS.md section 4, part C)."""

Column = Annotated[str, StringConstraints(pattern=COLUMN_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class GoldenRequest(RequestBody):
    """The request a correct resolution produces: a metric plus the body of docs section 7.1."""

    metric: Name

    def to_query_request(self) -> QueryRequest:
        return QueryRequest.from_example(self.metric, self)


class Candidate(_Strict):
    """One reading of a `clarify` question, with its own request and reference result."""

    metric: Name
    request: GoldenRequest
    answer_columns: Annotated[list[Column], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.request.metric != self.metric:
            raise ValueError(f"candidate {self.metric}: request.metric is {self.request.metric}")
        return self


class Expected(_Strict):
    metrics: list[Name] | None = None
    request: GoldenRequest | None = None
    answer_columns: list[Column] | None = None
    candidates: list[Candidate] | None = None
    reason: Reason | None = None
    nearest: Name | None = None


class Question(_Strict):
    id: Annotated[str, StringConstraints(pattern=ID_PATTERN)]
    question: Annotated[str, StringConstraints(min_length=5)]
    language: Language
    group: Group
    tags: list[Annotated[str, StringConstraints(pattern=TAG_PATTERN)]]
    outcome: Outcome
    expected: Expected
    notes: str | None = None

    @model_validator(mode="after")
    def _fields_by_outcome(self) -> Self:
        e = self.expected
        present = {name for name in Expected.model_fields if getattr(e, name) not in (None, [])}
        required, allowed = _FIELDS[self.outcome]
        missing = required - present
        extra = present - allowed
        if missing:
            raise ValueError(f"{self.id}: outcome {self.outcome} needs expected.{sorted(missing)}")
        if extra:
            raise ValueError(f"{self.id}: outcome {self.outcome} does not take {sorted(extra)}")
        if self.outcome == "answer":
            assert e.metrics is not None and e.request is not None
            if e.request.metric not in e.metrics:
                raise ValueError(f"{self.id}: request.metric is not in expected.metrics")
        elif self.outcome == "clarify":
            assert e.candidates is not None
            names = [c.metric for c in e.candidates]
            if len(names) < 2 or len(set(names)) != len(names):
                raise ValueError(f"{self.id}: clarify needs two or more different candidates")
        elif self.outcome == "no_metric":
            if e.reason == "personal_data":
                raise ValueError(f"{self.id}: personal_data belongs to outcome refuse")
            if (e.reason in ERROR_CODE_FOR_REASON) != (e.request is not None):
                raise ValueError(
                    f"{self.id}: expected.request is required for reasons "
                    f"{sorted(ERROR_CODE_FOR_REASON)} and not allowed for no_definition"
                )
        else:
            if e.reason != "personal_data":
                raise ValueError(f"{self.id}: refuse needs expected.reason personal_data")
        return self

    def reference_names(self) -> list[str]:
        """Stems of the reference SQL and CSV files this question needs."""
        if self.outcome == "answer":
            return [self.id]
        if self.outcome == "clarify":
            assert self.expected.candidates is not None
            return [f"{self.id}_{c.metric}" for c in self.expected.candidates]
        return []


_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "answer": ({"metrics", "request", "answer_columns"}, {"metrics", "request", "answer_columns"}),
    "clarify": ({"candidates"}, {"candidates"}),
    "no_metric": ({"reason"}, {"reason", "nearest", "request"}),
    "refuse": ({"reason"}, {"reason", "request"}),
}


def load_questions(path: Path) -> list[Question]:
    """Read and validate the questions file. Raises ValueError listing every problem."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: the file must be a list of questions")
    questions: list[Question] = []
    problems: list[str] = []
    for index, item in enumerate(raw, start=1):
        try:
            questions.append(Question.model_validate(item))
        except ValidationError as error:
            label = item.get("id", f"item {index}") if isinstance(item, dict) else f"item {index}"
            for detail in error.errors():
                where = ".".join(str(part) for part in detail["loc"])
                problems.append(f"{label}: {where}: {detail['msg']}")
    seen: set[str] = set()
    for question in questions:
        if question.id in seen:
            problems.append(f"{question.id}: id used twice")
        seen.add(question.id)
    if problems:
        raise ValueError(f"{path}: " + "; ".join(problems))
    return questions
