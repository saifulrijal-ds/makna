"""WP-10B criterion 6 and ruling 9 (D-29): who can set the governance mode.

Only the server configuration sets it. A request cannot (the request models forbid unknown
fields, and the compile functions take the policy as a keyword-only argument that the caller
builds from configuration), and `makna validate` (V19) always checks in `strict` mode.
"""

from __future__ import annotations

import inspect
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from makna.cli.main import main
from makna.compiler.compile import compile_plan, compile_request
from makna.compiler.plan import available_dimensions, plan_request
from makna.config import load_settings
from makna.definitions.loader import load_definitions
from makna.definitions.validate import validate
from makna.model.definitions import RequestBody
from makna.model.governance import STRICT, GovernancePolicy
from makna.model.request import ErrorCode, QueryError, QueryRequest, RequestFilter
from makna.services import examples
from makna.services.examples import check_example_requests

from .compiler_support import DEFINITIONS, lending, request

GOVERNANCE_FIELDS = (
    "governance",
    "mode",
    "policy",
    "min_group_size",
    "allow_personal_filters",
    "apply_masks",
    "allow_unmasked_personal",
)
ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@localhost/makna", "MAKNA_SECRET_KEY": "k"}


# ---- no request field sets the mode ---------------------------------------------------


def test_the_request_model_has_no_governance_field() -> None:
    for model in (QueryRequest, RequestFilter, RequestBody):
        assert not set(GOVERNANCE_FIELDS) & set(model.model_fields), model
    assert QueryRequest.model_config["extra"] == "forbid"


@pytest.mark.parametrize("field", GOVERNANCE_FIELDS)
@pytest.mark.parametrize("value", ["off", {"mode": "off"}, True, 0, "custom"])
def test_a_request_that_carries_a_governance_field_is_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError, match=field):
        QueryRequest.model_validate({"metric": "new_customer_count", field: value})
    with pytest.raises(ValidationError, match=field):
        RequestFilter.model_validate(
            {"dimension": "customer.city", "op": "=", "value": 1, field: value}
        )
    with pytest.raises(ValidationError, match=field):
        RequestBody.model_validate({field: value})


def test_a_request_filter_or_dimension_cannot_smuggle_a_setting() -> None:
    """The only free text of a request is a filter value and a dimension name; neither is read
    as a setting."""
    for text in ("governance.mode=off", "off", "mode: off"):
        with pytest.raises(QueryError) as caught:
            plan_request(lending(), request("new_customer_count", dimensions=[text]))
        assert caught.value.code == ErrorCode.UNKNOWN_DIMENSION


def test_the_policy_is_a_keyword_only_argument_that_defaults_to_strict() -> None:
    for function in (plan_request, compile_request, available_dimensions):
        parameter = inspect.signature(function).parameters["governance"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, function
        assert parameter.default is STRICT, function
        assert "min_group_size" not in inspect.signature(function).parameters, function
    assert "governance" not in inspect.signature(compile_plan).parameters  # the plan carries it


# ---- V19 is always strict -------------------------------------------------------------


def definitions_with_a_personal_example(tmp_path: Path) -> Path:
    """The sample definitions with an example request that selects `customer.full_name`, which
    only a policy that allows unmasked personal data would compile."""
    root = tmp_path / "defs"
    shutil.copytree(DEFINITIONS, root)
    metric = root / "lending" / "metrics" / "new_customer_count.yaml"
    text = metric.read_text()
    assert "customer.customer_segment]" in text
    metric.write_text(text.replace("customer.customer_segment]", "customer.full_name]", 1))
    return root


def configure(directory: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    (directory / "config").mkdir()
    (directory / "config" / "makna.toml").write_text(f"[governance]\n{body}\n")
    monkeypatch.chdir(directory)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


def test_v19_reports_the_same_whatever_mode_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = definitions_with_a_personal_example(tmp_path)
    reports = []
    for index, body in enumerate(
        [
            'mode = "strict"',
            'mode = "off"',
            'mode = "custom"\nallow_unmasked_personal = true',
            'mode = "custom"\nmin_group_size = 0\nallow_personal_filters = true',
        ]
    ):
        home = tmp_path / f"home{index}"
        home.mkdir()
        configure(home, body, monkeypatch)
        loaded = load_settings().governance.policy()
        issues = validate(load_definitions(root), extra_checks=[check_example_requests])
        assert [i.code for i in issues if i.code == "V19"], body
        assert main(["validate", str(root)]) == 1
        reports.append((loaded.mode, [i.message for i in issues], capsys.readouterr().out))
    assert [r[0] for r in reports] == ["strict", "off", "custom", "custom"]
    assert len({(tuple(r[1]), r[2]) for r in reports}) == 1  # the same result in every mode
    assert "PERSONAL_DATA_REFUSED" in reports[0][2]


def test_the_configured_policy_would_have_compiled_that_example(tmp_path: Path) -> None:
    """The control for the test above: the request V19 rejects does compile under `off`."""
    fields: dict[str, Any] = {"dimensions": ["customer.full_name"]}
    assert compile_request(
        lending(), request("new_customer_count", **fields), governance=GovernancePolicy.off()
    )
    with pytest.raises(QueryError, match="PERSONAL_DATA_REFUSED"):
        compile_request(lending(), request("new_customer_count", **fields))


def test_v19_takes_no_policy_and_the_example_check_plans_with_strict() -> None:
    assert "governance" not in inspect.signature(check_example_requests).parameters
    source = inspect.getsource(examples)
    assert "plan_request(model, request, enforce_status=False, governance=STRICT)" in source
