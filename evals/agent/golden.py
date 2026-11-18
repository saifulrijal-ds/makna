"""What the scorer needs of the golden set: each question with its expected results, read from
`evals/golden/questions.yaml`, the committed CSVs, and the metric formats of
`semantic/definitions/`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.compare import Table, read_expected_csv
from evals.golden.reference import EXPECTED_DIR, QUESTIONS_FILE
from evals.golden.schema import Question, load_questions
from makna.definitions.loader import load_definitions
from makna.model.definitions import Metric

DEFINITIONS_DIR = Path(__file__).resolve().parents[2] / "semantic" / "definitions"


@dataclass(frozen=True)
class Target:
    """One expected result: the committed CSV of a question (or of one candidate of a `clarify`
    question) cut to its answer columns."""

    stem: str
    metric: str
    keys: tuple[str, ...]
    """The answer columns that are not the value: text and date columns, in order."""
    value: str
    """The last answer column."""
    table: Table
    ratio: bool
    """The metric's value is a ratio shown as a share (`format.kind: percent`)."""
    currency: bool
    """The metric's value is money (`format.kind: currency`): absolute tolerance."""

    def rows(self) -> list[tuple[tuple[str, ...], str]]:
        """(key cells, value cell) of every expected row, in file order."""
        index = {name: self.table.columns.index(name) for name in (*self.keys, self.value)}
        return [
            (tuple(str(row[index[k]]) for k in self.keys), str(row[index[self.value]]))
            for row in self.table.rows
        ]


@dataclass(frozen=True)
class GoldenQuestion:
    question: Question
    targets: tuple[Target, ...]
    """`answer`: one target; `clarify`: one per candidate; otherwise none."""


class Golden:
    """The golden questions in file order, by id."""

    def __init__(self, questions: Sequence[GoldenQuestion]) -> None:
        self.questions = {q.question.id: q for q in questions}
        if len(self.questions) != len(questions):
            raise ValueError("a question id is used twice")
        self.order = tuple(q.question.id for q in questions)

    def __getitem__(self, question_id: str) -> GoldenQuestion:
        return self.questions[question_id]

    def __contains__(self, question_id: object) -> bool:
        return question_id in self.questions

    def __len__(self) -> int:
        return len(self.order)


def metric_formats(definitions_dir: Path = DEFINITIONS_DIR) -> dict[str, str]:
    """Metric name -> `format.kind` (`currency`, `number`, `count`, `percent`) of the official
    metric files (drafts are left out)."""
    kinds: dict[str, str] = {}
    for file in load_definitions(definitions_dir).files:
        if not file.is_proposal and isinstance(file.definition, Metric):
            kinds[file.definition.name] = file.definition.format.kind
    return kinds


def build_golden(
    questions: Sequence[Question],
    expected_dir: Path,
    kinds: Mapping[str, str],
) -> Golden:
    built: list[GoldenQuestion] = []
    for question in questions:
        targets: list[Target] = []
        e = question.expected
        specs: list[tuple[str, str, list[str]]] = []
        if question.outcome == "answer":
            assert e.request is not None and e.answer_columns is not None
            specs.append((question.id, e.request.metric, e.answer_columns))
        elif question.outcome == "clarify":
            for candidate in e.candidates or ():
                specs.append(
                    (
                        f"{question.id}_{candidate.metric}",
                        candidate.metric,
                        candidate.answer_columns,
                    )
                )
        for stem, metric, columns in specs:
            if metric not in kinds:
                raise ValueError(f"{question.id}: metric {metric} has no official definition")
            table = read_expected_csv(expected_dir / f"{stem}.csv")
            missing = [c for c in columns if c not in table.columns]
            if missing:
                raise ValueError(f"{stem}.csv has no column {missing[0]}")
            keys = tuple(columns[:-1])
            target = Target(
                stem,
                metric,
                keys,
                columns[-1],
                table,
                ratio=kinds[metric] == "percent",
                currency=kinds[metric] == "currency",
            )
            if len({k for k, _ in target.rows()}) != len(table.rows):
                raise ValueError(f"{stem}.csv: the key columns do not identify the rows")
            targets.append(target)
        built.append(GoldenQuestion(question, tuple(targets)))
    return Golden(built)


def load_golden(
    questions_file: Path = QUESTIONS_FILE,
    expected_dir: Path = EXPECTED_DIR,
    definitions_dir: Path = DEFINITIONS_DIR,
) -> Golden:
    return build_golden(
        load_questions(questions_file), expected_dir, metric_formats(definitions_dir)
    )
