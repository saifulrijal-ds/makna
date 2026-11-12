"""WP-10B criterion 1: with no policy argument and with `strict`, every request compiles to what
it compiled to before the governance modes.

`golden/compiler_governance_golden.json` was made by `golden/make_governance_golden.py` with the
compiler as it was at the base of WP-10B: 780 seeded requests over the sample model, plain and
with some columns tagged personal by the catalog, and for each the PostgreSQL text, result
columns, warnings, and explanation, or the error that was raised. The file is never regenerated
after a compiler change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import QueryError, QueryRequest

from .compiler_support import lending
from .golden.make_governance_golden import derived_model

GOLDEN = Path(__file__).parent / "golden" / "compiler_governance_golden.json"
CASES: list[dict[str, Any]] = json.loads(GOLDEN.read_text())["cases"]
MODELS = {"plain": lending(), "derived": derived_model(lending())}


def outcome(case: dict[str, Any], **options: Any) -> dict[str, Any]:
    model = MODELS[case["model"]]
    try:
        response = compile_request(model, QueryRequest.model_validate(case["request"]), **options)
    except QueryError as error:
        return {"error": str(error.code)}
    except GovernanceError:
        return {"error": "GovernanceError"}
    return {
        "sql": response.sql,
        "columns": [c.name for c in response.columns],
        "warnings": list(response.warnings),
        "explanation": list(response.explanation),
    }


def expected(case: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in case.items() if k not in ("model", "request")}


def test_the_golden_file_is_large_and_covers_the_cases_that_matter() -> None:
    assert len(CASES) >= 500
    compiled = [c for c in CASES if "sql" in c]
    errors = {c["error"] for c in CASES if "error" in c}
    assert len(compiled) >= 300
    assert {"PERSONAL_DATA_REFUSED", "UNKNOWN_DIMENSION", "DIMENSION_NOT_AVAILABLE"} <= errors
    personal = [c for c in compiled if "__groups_left_out" in c["columns"]]
    assert len(personal) >= 100  # personal dimensions with the minimum group size
    assert any("birth_date" in c["sql"] for c in compiled)
    assert any(c["request"]["filters"] for c in compiled)
    assert any(c["request"].get("grain") for c in compiled)
    assert any(c["request"].get("time_start") for c in compiled)
    assert {c["model"] for c in CASES} == {"plain", "derived"}


def test_every_golden_request_compiles_as_before_with_no_policy_argument() -> None:
    wrong = [i for i, case in enumerate(CASES) if outcome(case) != expected(case)]
    assert not wrong, f"cases {wrong[:10]} differ from the golden file"


def test_every_golden_request_compiles_as_before_with_the_strict_policy() -> None:
    wrong = [
        i for i, case in enumerate(CASES) if outcome(case, governance=STRICT) != expected(case)
    ]
    assert not wrong, f"cases {wrong[:10]} differ from the golden file"


@pytest.mark.parametrize("minimum", [10, 2, 25])
def test_strict_with_a_minimum_changes_only_the_minimum(minimum: int) -> None:
    """The minimum is the one thing `strict` lets the adopter set: the SQL text differs from the
    golden text only in the number, and the requests that were refused are still refused."""
    policy = GovernancePolicy.strict(minimum)
    for case in CASES:
        got = outcome(case, governance=policy)
        want = expected(case)
        if "sql" not in want:
            assert got == want
            continue
        assert got["columns"] == want["columns"]
        if minimum == 10:
            assert got == want
        else:
            replaced = want["sql"].replace(">= 10", f">= {minimum}").replace("< 10", f"< {minimum}")
            assert got["sql"] == replaced
