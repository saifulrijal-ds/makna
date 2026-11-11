"""WP-03 criterion 3: the Pydantic models and the JSON Schema agree."""

# ruff: noqa: E501 - the case tables below are one case per line

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from makna.model.definitions import dump_definition, parse_definition

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "semantic" / "schema" / "examples"
SCHEMA = json.loads((ROOT / "semantic" / "schema" / "makna.schema.json").read_text("utf-8"))
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
EXAMPLE_FILES = sorted(EXAMPLES.rglob("*.yaml"))

Doc = dict[str, Any]


def _example(relative: str) -> Doc:
    data = yaml.safe_load((EXAMPLES / relative).read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def _schema_accepts(doc: Any) -> bool:
    return SCHEMA_VALIDATOR.is_valid(doc)


def _model_accepts(doc: Any) -> bool:
    try:
        parse_definition(doc)
    except ValidationError:
        return False
    return True


def test_there_are_examples() -> None:
    assert len(EXAMPLE_FILES) == 16


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda p: str(p.relative_to(EXAMPLES)))
def test_every_example_round_trips(path: Path) -> None:
    original = yaml.safe_load(path.read_text("utf-8"))
    assert _schema_accepts(original)
    model = parse_definition(original)
    dumped = dump_definition(model)
    assert dumped == original
    assert _schema_accepts(dumped)
    # Loading the dump again gives the same model.
    assert parse_definition(dumped) == model


def _derived() -> Doc:
    doc = _example("lending/metrics/npf_ratio.yaml")
    for key in ("numerator", "denominator"):
        del doc[key]
    doc.update({"type": "derived", "expr": "a / NULLIF(b, 0)", "inputs": ["a_metric", "b_metric"]})
    return doc


def test_a_derived_metric_is_accepted_by_both() -> None:
    assert _schema_accepts(_derived())
    assert _model_accepts(_derived())


Mutation = Callable[[Doc], None]
METRIC_RATIO = "lending/metrics/npf_ratio.yaml"
METRIC_SIMPLE = "lending/metrics/npf_outstanding_principal.yaml"
ENTITY = "lending/entities/customer.yaml"
SNAPSHOT = "lending/entities/contract_snapshot.yaml"
DOMAIN = "lending/domain.yaml"
GLOSSARY = "lending/glossary/npf.yaml"
DERIVED = "(derived metric built in the test)"


def _set(path: list[str | int], value: Any) -> Mutation:
    def apply(doc: Doc) -> None:
        target: Any = doc
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value

    return apply


def _drop(path: list[str | int]) -> Mutation:
    def apply(doc: Doc) -> None:
        target: Any = doc
        for step in path[:-1]:
            target = target[step]
        del target[path[-1]]

    return apply


# (id, example it starts from, mutation). Every one must be rejected by both the schema and
# the models. The first three are the examples named in the acceptance criterion.
REJECTED: list[tuple[str, str, Mutation]] = [
    ("unknown-top-level-key", METRIC_SIMPLE, _set(["colour"], "red")),
    ("ratio-with-measure", METRIC_RATIO, _set(["measure"], "contract_snapshot.outstanding_principal")),
    ("metric-without-id-synonym", METRIC_RATIO, _drop(["synonyms", "id"])),
    ("metric-with-empty-id-synonyms", METRIC_RATIO, _set(["synonyms", "id"], [])),
    ("entity-without-en-synonym", ENTITY, _drop(["synonyms", "en"])),
    ("glossary-without-id-synonym", GLOSSARY, _drop(["synonyms", "id"])),
    ("unknown-key-in-dimension", ENTITY, _set(["dimensions", 0, "colour"], "red")),
    ("unknown-key-in-owner", ENTITY, _set(["owner", "phone"], "1")),
    ("unknown-key-in-source", ENTITY, _set(["source", "schema"], "x")),
    ("wrong-api-version", ENTITY, _set(["apiVersion"], "makna/v2")),
    ("unknown-kind", ENTITY, _set(["kind"], "cube")),
    ("missing-kind", ENTITY, _drop(["kind"])),
    ("missing-owner", ENTITY, _drop(["owner"])),
    ("missing-status", METRIC_SIMPLE, _drop(["status"])),
    ("bad-status", METRIC_SIMPLE, _set(["status"], "published")),
    ("bad-sensitivity", ENTITY, _set(["sensitivity"], "secret")),
    ("name-with-capital", ENTITY, _set(["name"], "Customer")),
    ("name-too-short", ENTITY, _set(["name"], "c")),
    ("name-is-number", ENTITY, _set(["name"], 5)),
    ("short-object-description", ENTITY, _set(["description"], "Too short")),
    ("short-dimension-description", ENTITY, _set(["dimensions", 0, "description"], "short")),
    ("empty-label", ENTITY, _set(["label"], "")),
    ("empty-owner-team", ENTITY, _set(["owner", "team"], "")),
    ("version-zero", ENTITY, _set(["version"], 0)),
    ("version-is-text", ENTITY, _set(["version"], "1")),
    ("version-is-bool", ENTITY, _set(["version"], True)),
    ("null-contact", ENTITY, _set(["owner", "contact"], None)),
    ("null-tags", ENTITY, _set(["tags"], None)),
    ("duplicate-tags", ENTITY, _set(["tags"], ["a", "a"])),
    ("bad-synonym-language", ENTITY, _set(["synonyms", "eng"], ["x"])),
    ("duplicate-synonyms", ENTITY, _set(["synonyms", "en"], ["a", "a"])),
    ("empty-synonym-word", ENTITY, _set(["synonyms", "en"], [""])),
    ("synonyms-not-a-list", ENTITY, _set(["synonyms", "en"], "borrower")),
    ("bad-table", ENTITY, _set(["source", "table"], "contracts")),
    ("empty-primary-key", ENTITY, _set(["primary_key"], [])),
    ("duplicate-primary-key", ENTITY, _set(["primary_key"], ["a", "a"])),
    ("missing-dimensions", ENTITY, _drop(["dimensions"])),
    ("bad-dimension-type", ENTITY, _set(["dimensions", 0, "type"], "text")),
    ("empty-dimension-expr", ENTITY, _set(["dimensions", 0, "expr"], "")),
    ("masking-without-method", ENTITY, _set(["dimensions", 4, "masking"], {"grain": "year"})),
    ("masking-unknown-method", ENTITY, _set(["dimensions", 4, "masking"], {"method": "hash"})),
    ("truncate-date-without-grain", ENTITY, _set(["dimensions", 4, "masking"], {"method": "truncate_date"})),
    ("truncate-date-bad-grain", ENTITY, _set(["dimensions", 4, "masking"], {"method": "truncate_date", "grain": "day"})),
    ("bucket-without-width", ENTITY, _set(["dimensions", 4, "masking"], {"method": "bucket"})),
    ("bucket-zero-width", ENTITY, _set(["dimensions", 4, "masking"], {"method": "bucket", "width": 0})),
    ("redact-with-width", ENTITY, _set(["dimensions", 4, "masking"], {"method": "redact", "width": 1})),
    ("value-without-value-key", ENTITY, _set(["dimensions", 0, "values"], [{"description": "x"}])),
    ("value-is-list", ENTITY, _set(["dimensions", 0, "values"], [{"value": ["a"]}])),
    ("measure-bad-agg", ENTITY, _set(["measures", 0, "agg"], "median")),
    ("measure-sum-without-expr", SNAPSHOT, _drop(["measures", 0, "expr"])),
    ("measure-without-time-dimension", SNAPSHOT, _drop(["measures", 0, "time_dimension"])),
    ("measure-bad-semi-additive", SNAPSHOT, _set(["measures", 0, "semi_additive"], "first")),
    ("measure-rule-qualified", SNAPSHOT, _set(["measures", 1, "rule"], "contract_snapshot.non_performing")),
    ("measure-unknown-key", SNAPSHOT, _set(["measures", 0, "unit"], "IDR")),
    ("relationship-without-keys", ENTITY, _set(["relationships", 0, "keys"], [])),
    ("relationship-bad-type", ENTITY, _set(["relationships", 0, "type"], "many_to_many")),
    ("relationship-key-unknown-field", ENTITY, _set(["relationships", 0, "keys"], [{"from": "a", "to": "b", "x": 1}])),
    ("relationship-key-missing-to", ENTITY, _set(["relationships", 0, "keys"], [{"from": "a"}])),
    ("rule-without-expr", SNAPSHOT, _drop(["rules", 0, "expr"])),
    ("rule-short-description", SNAPSHOT, _set(["rules", 0, "description"], "short")),
    ("metric-bad-type", METRIC_SIMPLE, _set(["type"], "complex")),
    ("simple-without-measure", METRIC_SIMPLE, _drop(["measure"])),
    ("simple-with-numerator", METRIC_SIMPLE, _set(["numerator"], "outstanding_principal")),
    ("simple-with-expr", METRIC_SIMPLE, _set(["expr"], "a + b")),
    ("simple-with-inputs", METRIC_SIMPLE, _set(["inputs"], ["a"])),
    ("measure-not-qualified", METRIC_SIMPLE, _set(["measure"], "outstanding_principal")),
    ("ratio-without-denominator", METRIC_RATIO, _drop(["denominator"])),
    ("ratio-with-filters", METRIC_RATIO, _set(["filters"], [])),
    ("ratio-with-join-paths", METRIC_RATIO, _set(["join_paths"], {})),
    ("ratio-with-expr", METRIC_RATIO, _set(["expr"], "a / b")),
    ("derived-without-inputs", DERIVED, _drop(["inputs"])),
    ("derived-with-numerator", DERIVED, _set(["numerator"], "a_metric")),
    ("derived-with-measure", DERIVED, _set(["measure"], "a.b")),
    ("derived-empty-inputs", DERIVED, _set(["inputs"], [])),
    ("derived-duplicate-inputs", DERIVED, _set(["inputs"], ["a_metric", "a_metric"])),
    ("metric-without-format", METRIC_RATIO, _drop(["format"])),
    ("format-bad-kind", METRIC_RATIO, _set(["format", "kind"], "money")),
    ("format-decimals-too-large", METRIC_RATIO, _set(["format", "decimals"], 7)),
    ("format-decimals-negative", METRIC_RATIO, _set(["format", "decimals"], -1)),
    ("format-decimals-float", METRIC_RATIO, _set(["format", "decimals"], 2.5)),
    ("empty-time-grains", METRIC_RATIO, _set(["time_grains"], [])),
    ("bad-time-grain", METRIC_RATIO, _set(["time_grains"], ["month", "decade"])),
    ("duplicate-time-grains", METRIC_RATIO, _set(["time_grains"], ["month", "month"])),
    ("bad-default-grain", METRIC_RATIO, _set(["default_grain"], "decade")),
    ("short-example-question", METRIC_RATIO, _set(["example_questions"], ["NPF"])),
    ("empty-example-requests", METRIC_RATIO, _set(["example_requests"], [])),
    ("example-request-without-request", METRIC_RATIO, _drop(["example_requests", 0, "request"])),
    ("example-request-with-metric", METRIC_RATIO, _set(["example_requests", 0, "request", "metric"], "npf_ratio")),
    ("example-request-bad-grain", METRIC_RATIO, _set(["example_requests", 0, "request", "grain"], "decade")),
    ("example-request-bad-date", METRIC_RATIO, _set(["example_requests", 0, "request", "time_start"], "2026-13-01")),
    ("example-request-date-not-string", METRIC_RATIO, _set(["example_requests", 0, "request", "time_start"], 20260101)),
    ("example-request-limit-zero", METRIC_RATIO, _set(["example_requests", 0, "request", "limit"], 0)),
    ("example-request-limit-too-large", METRIC_RATIO, _set(["example_requests", 0, "request", "limit"], 10001)),
    ("example-request-unqualified-dimension", METRIC_RATIO, _set(["example_requests", 0, "request", "dimensions"], ["region_name"])),
    ("filter-in-with-scalar", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.b", "op": "in", "value": "x"}])),
    ("filter-in-with-empty-list", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.b", "op": "in", "value": []}])),
    ("filter-equals-with-list", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.b", "op": "=", "value": ["x"]}])),
    ("filter-equals-without-value", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.b", "op": "="}])),
    ("filter-is-null-with-value", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.bc", "op": "is_null", "value": 1}])),
    ("filter-null-value", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.bc", "op": "=", "value": None}])),
    ("filter-bad-op", METRIC_SIMPLE, _set(["filters"], [{"dimension": "a.bc", "op": "like", "value": "x"}])),
    ("filter-unqualified-dimension", METRIC_SIMPLE, _set(["filters"], [{"dimension": "abc", "op": "=", "value": 1}])),
    ("filter-and-rule-together", METRIC_SIMPLE, _set(["filters"], [{"dimension": "ab.cd", "op": "=", "value": 1, "rule": "ab.cd"}])),
    ("rule-ref-unqualified", METRIC_SIMPLE, _set(["filters"], [{"rule": "non_performing"}])),
    ("join-paths-empty-chain", METRIC_SIMPLE, _set(["join_paths", "branch"], [])),
    ("join-paths-bad-key", METRIC_SIMPLE, _set(["join_paths", "Branch"], ["contract.servicing_branch"])),
    ("join-paths-unqualified-step", METRIC_SIMPLE, _set(["join_paths", "branch"], ["servicing_branch"])),
    ("deprecated-metric-without-deprecation", METRIC_SIMPLE, _set(["status"], "deprecated")),
    ("deprecation-without-reason", METRIC_SIMPLE, _set(["deprecation"], {"replaced_by": "metric:x"})),
    ("deprecation-empty-reason", METRIC_SIMPLE, _set(["deprecation"], {"reason": ""})),
    ("domain-with-domain", DOMAIN, _set(["domain"], "lending")),
    ("glossary-bad-related", GLOSSARY, _set(["related"], ["npf_ratio"])),
    ("glossary-related-unknown-kind", GLOSSARY, _set(["related"], ["dimension:abc"])),
    ("glossary-duplicate-related", GLOSSARY, _set(["related"], ["metric:npf_ratio", "metric:npf_ratio"])),
    ("glossary-without-domain", GLOSSARY, _drop(["domain"])),
]  # fmt: skip

ACCEPTED: list[tuple[str, str, Mutation]] = [
    ("no-optional-keys", METRIC_SIMPLE, _drop(["join_paths"])),
    ("deprecated-entity-without-deprecation", ENTITY, _set(["status"], "deprecated")),
    ("deprecated-metric-with-deprecation", METRIC_SIMPLE, _set(["status"], "deprecated")),
    ("count-measure-without-expr", ENTITY, lambda doc: None),
    ("domain-without-synonyms", DOMAIN, _drop(["synonyms"])),
    ("domain-with-japanese-synonyms", DOMAIN, _set(["synonyms", "ja"], ["x"])),
    ("filter-in-list", METRIC_SIMPLE, _set(["filters"], [{"dimension": "ab.cd", "op": "in", "value": ["x", 1, True]}])),
    ("filter-is-not-null", METRIC_SIMPLE, _set(["filters"], [{"dimension": "ab.cd", "op": "is_not_null"}])),
    ("filter-rule-ref", METRIC_SIMPLE, _set(["filters"], [{"rule": "contract_snapshot.non_performing"}])),
    ("bucket-masking", ENTITY, _set(["dimensions", 4, "masking"], {"method": "bucket", "width": 10})),
    ("bucket-masking-float-width", ENTITY, _set(["dimensions", 4, "masking"], {"method": "bucket", "width": 0.5})),
    ("redact-masking", ENTITY, _set(["dimensions", 4, "masking"], {"method": "redact"})),
    ("numeric-and-boolean-values", ENTITY, _set(["dimensions", 0, "values"], [{"value": 1}, {"value": False}, {"value": 2.5}])),
    ("one-to-one-relationship", ENTITY, _set(["relationships", 0, "type"], "one_to_one")),
]  # fmt: skip


def _build(start: str, change: Mutation) -> Doc:
    if start == DERIVED:
        doc = _derived()
    else:
        doc = copy.deepcopy(_example(start))
    change(doc)
    return doc


def _fix_deprecated(doc: Doc) -> None:
    if doc.get("status") == "deprecated" and "deprecation" not in doc and doc["kind"] == "metric":
        doc["deprecation"] = {"reason": "Replaced."}


@pytest.mark.parametrize(("case", "start", "change"), REJECTED, ids=[c[0] for c in REJECTED])
def test_a_property_the_schema_forbids_is_rejected_by_both(
    case: str, start: str, change: Mutation
) -> None:
    doc = _build(start, change)
    assert not _schema_accepts(doc), "the schema accepted a document that should be rejected"
    assert not _model_accepts(doc), "the models accepted a document the schema rejects"


@pytest.mark.parametrize(("case", "start", "change"), ACCEPTED, ids=[c[0] for c in ACCEPTED])
def test_a_property_the_schema_allows_is_accepted_by_both(
    case: str, start: str, change: Mutation
) -> None:
    doc = _build(start, change)
    if case == "deprecated-metric-with-deprecation":
        _fix_deprecated(doc)
    assert _schema_accepts(doc), "the schema rejected a document that should be accepted"
    assert _model_accepts(doc), "the models rejected a document the schema accepts"
