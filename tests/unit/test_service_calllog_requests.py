"""WP-12 criterion 6 (the pure part): what the tool-call log records of a request.

A filter value is kept only when its dimension is known to be non-personal in the approved
snapshot of a known metric, whatever the outcome. A call refused with PERSONAL_DATA_REFUSED
records names the model knows and nothing else.
"""

from __future__ import annotations

import json
from typing import Any

from makna.compiler.snapshot import Model
from makna.model.request import QueryRequest
from makna.services.calllog import CallLog
from makna.services.redact import REDACTED

from .compiler_support import lending


def model(derived: dict[tuple[str, str], str] | None = None) -> Model:
    base = lending()
    return Model.build(
        list(base.entities.values()),
        [m for m in base.metrics.values() if m.status != "draft"],
        derived_sensitivity=derived,
    )


def filters_of(request: dict[str, Any], snapshot: Model | None) -> list[dict[str, Any]]:
    return CallLog.compile_request(request, snapshot)["filters"]  # type: ignore[no-any-return]


def test_a_value_on_a_non_personal_dimension_is_kept() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "branch.region_name", "op": "=", "value": "Jawa Barat"}],
    }
    assert filters_of(request, model())[0]["value"] == "Jawa Barat"


def test_a_value_on_a_declared_personal_dimension_is_removed() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "customer.birth_year", "op": "=", "value": "1980-01-01"}],
    }
    assert filters_of(request, model())[0]["value"] == REDACTED


def test_a_value_on_a_catalog_derived_personal_dimension_is_removed() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "customer.city", "op": "=", "value": "Bandung"}],
    }
    assert filters_of(request, model())[0]["value"] == "Bandung"
    tagged = model({("lending.core.customers", "city"): "personal"})
    assert filters_of(request, tagged)[0]["value"] == REDACTED


def test_a_value_on_an_unknown_dimension_is_removed() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "customer.shoe_size", "op": "=", "value": "42"}],
    }
    assert filters_of(request, model())[0]["value"] == REDACTED


def test_every_value_is_removed_for_an_unknown_metric_or_without_a_snapshot() -> None:
    filters = [{"dimension": "branch.region_name", "op": "=", "value": "Jawa Barat"}]
    assert filters_of({"metric": "no_such_metric", "filters": filters}, model())[0]["value"] == (
        REDACTED
    )
    assert filters_of({"metric": "npf_ratio", "filters": filters}, None)[0]["value"] == REDACTED


def test_a_draft_metric_is_not_known_to_the_approved_snapshot() -> None:
    filters = [{"dimension": "branch.region_name", "op": "=", "value": "Jawa Barat"}]
    request = {"metric": "write_off_amount", "filters": filters}
    assert filters_of(request, model())[0]["value"] == REDACTED


def test_list_values_are_removed_as_a_whole_for_a_personal_dimension() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [{"dimension": "customer.birth_year", "op": "in", "value": ["1980", "1981"]}],
    }
    assert filters_of(request, model())[0]["value"] == REDACTED


def test_digits_emails_and_phones_in_a_kept_value_are_still_redacted() -> None:
    request = {
        "metric": "npf_ratio",
        "filters": [
            {"dimension": "branch.branch_name", "op": "=", "value": "a@b.com 4111 1111 1111 1111"}
        ],
    }
    value = filters_of(request, model())[0]["value"]
    assert value == f"{REDACTED} {REDACTED}"


def test_free_text_in_every_field_of_a_bad_request_is_redacted() -> None:
    request = {
        "metric": "john@example.com",
        "dimensions": ["+62 812-3456-7890"],
        "grain": "4111 1111 1111 1111",
        "time_start": "mail a@b.com",
        "time_end": "0812 3456 7890",
        "limit": "many",
        "filters": [{"dimension": "x@y.com", "op": "=", "value": "v"}, "not a filter"],
        "extra_key_with_secret": "x",
    }
    stored = json.dumps(CallLog.compile_request(request, model()))
    for leak in ("john", "example.com", "812", "4111", "a@b", "x@y", "secret", "3456"):
        assert leak not in stored, leak
    assert "many" not in stored  # a wrong type is marked, not copied


def test_an_object_that_is_not_a_request_is_marked_invalid() -> None:
    assert CallLog.compile_request("select 1", model()) == {"request": "[invalid]"}
    assert CallLog.compile_request(None, model()) == {"request": "[invalid]"}


def test_a_valid_request_object_is_recorded_like_its_dict() -> None:
    parsed = QueryRequest(
        metric="npf_ratio",
        dimensions=("branch.region_name",),
        grain="quarter",
        time_start="2025-10-01",
    )
    stored = CallLog.compile_request(parsed, model())
    assert stored["metric"] == "npf_ratio"
    assert stored["dimensions"] == ["branch.region_name"]
    assert stored["time_start"] == "2025-10-01"


def test_a_refusal_records_only_names_the_model_knows() -> None:
    request = QueryRequest(
        metric="npf_ratio",
        dimensions=("customer.full_name", "mail a@b.com"),
        filters=(
            {"dimension": "customer.birth_year", "op": "=", "value": "1980-01-01"},
            {"dimension": "john.smith", "op": "=", "value": "x"},
        ),
        grain="month",
        time_start="2025-01-01",
        limit=5,
    )
    stored = CallLog.refusal_request(request, model())
    assert stored == {
        "metric": "npf_ratio",
        "dimensions": ["customer.full_name", REDACTED],
        "filters": [{"dimension": "customer.birth_year"}, {"dimension": REDACTED}],
    }
    text = json.dumps(stored)
    for left_out in ("1980", "a@b", "john", "month", "2025"):
        assert left_out not in text


def test_a_refusal_for_an_unknown_metric_or_without_a_snapshot_keeps_no_name() -> None:
    request = QueryRequest(metric="john smith")
    assert CallLog.refusal_request(request, model())["metric"] == REDACTED
    assert CallLog.refusal_request(request, None) == {}
    assert CallLog.refusal_request(None, model()) == {}


def test_search_and_id_requests_are_redacted_and_marked_when_malformed() -> None:
    stored = CallLog.search_request(
        "NPF a@b.com 4111 1111 1111 1111", ["metric", "john@x.com"], "lending", True, 5
    )
    assert stored["query"] == f"NPF {REDACTED} {REDACTED}"
    assert stored["kinds"] == ["metric", REDACTED]
    assert CallLog.search_request(5, "metric", 5, "yes", "ten") == {
        "query": "[invalid]",
        "kinds": "[invalid]",
        "domain": "[invalid]",
        "include_deprecated": "[invalid]",
        "limit": "[invalid]",
    }
    assert CallLog.id_request("metric:12345678901", detail="full") == {
        "id": f"metric:{REDACTED}",
        "detail": "full",
    }
    assert CallLog.id_request("x", include_deprecated=True)["include_deprecated"] is True
