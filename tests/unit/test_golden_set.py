"""WP-14 (EVL-1): the golden question set, checked against the schema, the definitions, and the
files beside it. The reference SQL itself runs in the engine tier (`tests/engine`)."""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest

from evals.golden.reference import (
    EXPECTED_DIR,
    MAX_ROWS,
    MIN_ROWS,
    QUESTIONS_FILE,
    SQL_DIR,
    ReferenceResult,
    render_csv,
    row_count_problem,
)
from evals.golden.schema import ERROR_CODE_FOR_REASON, GROUPS, Question, load_questions
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.definitions.loader import load_definitions
from makna.model.definitions import Metric
from makna.model.request import QueryError, QueryRequest
from makna.services.examples import model_from_definitions

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = ROOT / "semantic" / "definitions"
EVALS_DOC = ROOT / "docs" / "EVALS.md"

# docs/EVALS.md section 2.4: the least each group may hold, and the least the set may hold.
MINIMUM_PER_GROUP = {
    "direct_english": 8,
    "direct_indonesian_or_mixed": 10,
    "dimensions_and_filters": 8,
    "time_handling": 6,
    "governed_default": 4,
    "clarify": 6,
    "no_metric": 6,
    "refuse": 3,
}
MINIMUM_TOTAL = 50


@pytest.fixture(scope="module")
def questions() -> list[Question]:
    return load_questions(QUESTIONS_FILE)


@pytest.fixture(scope="module")
def metrics() -> dict[str, Metric]:
    found = {}
    for file in load_definitions(DEFINITIONS).valid_files():
        if isinstance(file.definition, Metric):
            found[file.definition.name] = file.definition
    return found


def requests_of(question: Question) -> Iterator[QueryRequest]:
    """Every request a question names, as the planner sees it."""
    e = question.expected
    if e.request is not None:
        yield e.request.to_query_request()
    for candidate in e.candidates or ():
        yield candidate.request.to_query_request()


def expected_metric_names(question: Question) -> set[str]:
    e = question.expected
    names = set(e.metrics or ())
    names.update(c.metric for c in e.candidates or ())
    if e.nearest:
        names.add(e.nearest)
    if e.request is not None:
        names.add(e.request.metric)
    return names


def normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


# ---- criterion 1: schema, size, counts per group ----------------------------------------------


def test_the_file_validates_and_is_big_enough(questions: list[Question]) -> None:
    assert len(questions) >= MINIMUM_TOTAL
    ids = [q.id for q in questions]
    assert len(set(ids)) == len(ids)
    assert ids[:20] == [f"G{n:02d}" for n in range(1, 21)]


def test_every_group_meets_the_composition_table(questions: list[Question]) -> None:
    assert set(MINIMUM_PER_GROUP) == set(GROUPS)
    counts: Counter[str] = Counter(q.group for q in questions)
    short = {g: (counts[g], least) for g, least in MINIMUM_PER_GROUP.items() if counts[g] < least}
    assert short == {}


def test_a_group_matches_its_outcome(questions: list[Question]) -> None:
    outcome_groups = {"clarify": "clarify", "no_metric": "no_metric", "refuse": "refuse"}
    for q in questions:
        if q.outcome in outcome_groups:
            assert q.group == outcome_groups[q.outcome], q.id
        else:
            assert q.group not in outcome_groups, q.id


def test_no_metric_questions_cover_the_three_reasons(questions: list[Question]) -> None:
    reasons = Counter(q.expected.reason for q in questions if q.outcome == "no_metric")
    assert reasons["no_definition"] >= 3
    assert reasons["dimension_not_available"] >= 2
    assert reasons["not_approved"] >= 1


def test_the_three_early_questions_are_there(questions: list[Question]) -> None:
    by_text = {q.question: q for q in questions}
    hapus = by_text["Berapa hapus buku tahun lalu?"]
    assert (hapus.outcome, hapus.expected.reason) == ("no_metric", "not_approved")
    nik = by_text["Total disbursement for customer with NIK 9900000000000001"]
    assert nik.outcome == "refuse"
    jabar = by_text["Outstanding for Jawa Barat"]
    assert jabar.outcome == "answer"
    assert jabar.expected.metrics == ["outstanding_principal"]
    assert jabar.expected.request is not None
    assert [f.model_dump() for f in jabar.expected.request.filters or ()] == [
        {"dimension": "branch.region_name", "op": "=", "value": "Jawa Barat"}
    ]


def test_the_first_20_are_the_table_of_the_design(questions: list[Question]) -> None:
    """EVALS 2.5: G01 to G20 go into the file unchanged (text, language, outcome, metric)."""
    rows = {}
    for line in EVALS_DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and re.fullmatch(r"G\d\d", cells[0]):
            rows[cells[0]] = cells
    assert sorted(rows) == [f"G{n:02d}" for n in range(1, 21)]
    by_id = {q.id: q for q in questions}
    for qid, (_, text, language, outcome, metric_cell, *_rest) in rows.items():
        q = by_id[qid]
        assert q.question == text, qid
        assert (q.language, q.outcome) == (language, outcome), qid
        named = re.findall(r"`([a-z0-9_]+)`", metric_cell)
        if outcome == "answer":
            assert q.expected.metrics == named, qid
        elif outcome == "clarify":
            assert [c.metric for c in q.expected.candidates or ()] == named, qid


# ---- criterion 2: every approved metric is the expected metric of an answer question -----------


def test_approved_metrics_are_counted(metrics: dict[str, Metric]) -> None:
    approved = [m for m in metrics.values() if m.status == "approved"]
    assert len(approved) == 23, "docs/EVALS.md and docs/SAMPLE_DOMAIN.md say 23 approved metrics"


def test_every_approved_metric_has_an_answer_question(
    questions: list[Question], metrics: dict[str, Metric]
) -> None:
    approved = {name for name, m in metrics.items() if m.status == "approved"}
    covered = {m for q in questions if q.outcome == "answer" for m in q.expected.metrics or ()}
    assert approved - covered == set()
    assert covered - approved == set(), "an answer question names a metric that is not approved"


def test_metric_names_in_the_file_exist(
    questions: list[Question], metrics: dict[str, Metric]
) -> None:
    for q in questions:
        assert expected_metric_names(q) <= set(metrics), q.id


# ---- criterion 4: no leaks from the definitions ------------------------------------------------


def test_no_question_contains_the_name_of_its_metric(
    questions: list[Question], metrics: dict[str, Metric]
) -> None:
    for q in questions:
        for name in expected_metric_names(q) & set(metrics):
            assert name not in q.question.casefold(), (q.id, name)


def test_no_question_repeats_an_example_question(
    questions: list[Question], metrics: dict[str, Metric]
) -> None:
    examples: set[str] = set()
    for metric in metrics.values():
        examples.update(normal(t) for t in metric.example_questions or ())
        examples.update(normal(e.question) for e in metric.example_requests or ())
    assert examples
    leaked = [q.id for q in questions if normal(q.question) in examples]
    assert leaked == []


def test_no_expected_request_repeats_an_example_request(
    questions: list[Question], metrics: dict[str, Metric]
) -> None:
    for q in questions:
        for request in requests_of(q):
            for example in metrics[request.metric].example_requests or ():
                assert request != QueryRequest.from_example(request.metric, example.request), (
                    q.id,
                    request.metric,
                )


# ---- criterion 5: requests plan, and the refusals fail with the right code ---------------------


@pytest.fixture(scope="module")
def model() -> Model:
    # Built as the example-request check builds it: official files, then proposals, so the draft
    # `write_off_amount` is known (NOT_APPROVED, not UNKNOWN_METRIC).
    return model_from_definitions(load_definitions(DEFINITIONS))


def test_the_draft_metric_is_in_the_snapshot(model: Model) -> None:
    assert model.metrics["write_off_amount"].status == "draft"


def test_every_answer_and_clarify_request_plans(questions: list[Question], model: Model) -> None:
    planned = 0
    for q in questions:
        if q.outcome in ("answer", "clarify"):
            for request in requests_of(q):
                plan_request(model, request)
                planned += 1
    assert planned >= 40


def test_refusal_requests_fail_planning_with_the_matching_code(
    questions: list[Question], model: Model
) -> None:
    checked = []
    for q in questions:
        reason = q.expected.reason
        if q.outcome != "no_metric" or reason not in ERROR_CODE_FOR_REASON:
            continue
        (request,) = requests_of(q)
        with pytest.raises(QueryError) as caught:
            plan_request(model, request)
        assert str(caught.value.code) == ERROR_CODE_FOR_REASON[reason], q.id
        checked.append(q.id)
    assert len(checked) >= 3


def test_a_no_definition_question_names_no_request(questions: list[Question]) -> None:
    for q in questions:
        if q.expected.reason == "no_definition":
            assert q.expected.request is None, q.id


# ---- reference files: one SQL and one CSV per answer and per clarify candidate -----------------


def needed_names(questions: list[Question]) -> set[str]:
    return {name for q in questions for name in q.reference_names()}


def test_every_needed_sql_and_csv_exists_and_nothing_else(questions: list[Question]) -> None:
    needed = needed_names(questions)
    sql = {p.stem for p in SQL_DIR.glob("*.sql")}
    expected = {p.stem for p in EXPECTED_DIR.glob("*.csv")}
    assert needed - sql == set(), "missing reference SQL"
    assert needed - expected == set(), "missing expected CSV"
    assert sql - needed == set(), "reference SQL that no question uses"
    assert expected - needed == set(), "expected CSV that no question uses"
    assert {p.name for p in SQL_DIR.iterdir()} == {f"{n}.sql" for n in sql}
    assert {p.name for p in EXPECTED_DIR.iterdir()} == {f"{n}.csv" for n in expected}


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    text = path.read_bytes().decode("utf-8")
    assert "\r" not in text and text.endswith("\n"), path.name
    rows = list(csv.reader(text.splitlines()))
    return rows[0], rows[1:]


def test_expected_csv_files_are_well_formed(questions: list[Question]) -> None:
    for name in sorted(needed_names(questions)):
        header, rows = read_csv(EXPECTED_DIR / f"{name}.csv")
        assert MIN_ROWS <= len(rows) <= MAX_ROWS, name
        assert len(set(header)) == len(header), name
        assert all(len(row) == len(header) for row in rows), name


def test_answer_columns_are_columns_of_the_expected_csv(questions: list[Question]) -> None:
    for q in questions:
        e = q.expected
        if q.outcome == "answer":
            pairs = [(q.id, e.answer_columns or [], e.request)]
        elif q.outcome == "clarify":
            pairs = [
                (f"{q.id}_{c.metric}", c.answer_columns, c.request) for c in e.candidates or ()
            ]
        else:
            continue
        for name, columns, request in pairs:
            assert request is not None
            header, _ = read_csv(EXPECTED_DIR / f"{name}.csv")
            assert set(columns) <= set(header), name
            assert request.metric in header, name
            assert request.metric in columns, name
            for dimension in request.dimensions or ():
                assert dimension.replace(".", "__") in columns, name
            assert ("metric_time" in header) == (request.grain is not None), name
            if request.grain is not None:
                assert "metric_time" in columns, name


def test_reference_sql_is_hand_written_in_the_agreed_shape() -> None:
    for path in sorted(SQL_DIR.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        _, rows = read_csv(EXPECTED_DIR / f"{path.stem}.csv")
        first = text.splitlines()[0]
        assert first.startswith(f"-- {path.stem.split('_')[0]} "), path.name
        assert "Rules:" in text.split("\n\n")[0] or "Rules:" in "\n".join(text.splitlines()[:8])
        body = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
        assert "lending.core" not in body, f"{path.name}: use the core schema name"
        assert "SUM(" in body or "COUNT(" in body, path.name
        if len(rows) > 1:
            assert re.search(r"ORDER BY[^;]*\Z", body.strip(), re.S), f"{path.name}: needs ORDER BY"


# ---- the CSV writer ---------------------------------------------------------------------------


def test_render_csv_uses_the_agreed_text_forms() -> None:
    import datetime
    from decimal import Decimal

    result = ReferenceResult(
        ("a", "b", "c", "d", "e"),
        (
            (Decimal("10.50"), 3, 0.25, datetime.date(2026, 6, 1), None),
            ("x,y", True, 1.0, datetime.date(2026, 7, 1), "z"),
        ),
    )
    assert render_csv(result) == (
        'a,b,c,d,e\n10.50,3,0.25,2026-06-01,\n"x,y",true,1.0,2026-07-01,z\n'
    )
    with pytest.raises(TypeError):
        render_csv(ReferenceResult(("a",), ((object(),),)))


def test_row_count_limits() -> None:
    assert row_count_problem("x", ReferenceResult(("a",), ())) is not None
    assert row_count_problem("x", ReferenceResult(("a",), ((1,),))) is None
    assert (
        row_count_problem("x", ReferenceResult(("a",), tuple((i,) for i in range(201)))) is not None
    )
