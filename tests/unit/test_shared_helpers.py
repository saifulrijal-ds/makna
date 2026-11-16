"""WP-15B criteria 1 and 2 (C-7): each shared definition helper exists once.

The columns an expression reads, a metric and what it depends on, and the unit of a metric are
written in `makna.model` (`expressions.py`, `metrics.py`) and nowhere else. The scan below parses
every module of `src/makna` and fails when
- a function with the name of a removed copy is defined again (the names, and where a name is
  still legitimate, are in `REMOVED`);
- one of the shared functions is defined more than once;
- the unit logic is written inline again: an attribute access `<x>.format.unit` outside
  `makna/model/metrics.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from makna.model.definitions import Metric
from makna.model.metrics import (
    MetricDependencyError,
    dependency_closure,
    direct_inputs,
    metric_unit,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "makna"

# Removed copies: name -> the only files (relative to src/makna) where a function of that name
# may still be defined, because it is something else there.
REMOVED: dict[str, tuple[str, ...]] = {
    # the columns an expression reads (sensitivity, catalog.references, definitions.validate)
    "columns_read": ("model/expressions.py",),
    "_columns": (),
    # a metric and what it depends on (compiler.plan, services.definitions, services.service)
    "_closure": (),
    "_metric_closure": (),
    "_direct_inputs": (),
    "_metric_inputs": (),
    # the unit of a metric; `compiler/governance.py` has a `_unit` of its own: the grain of a
    # time truncation
    "_unit": ("compiler/governance.py",),
}
# `plan._columns` is the list of result columns of a plan, not a copy of the above, and keeps its
# name; it is the one `_columns` allowed.
ALLOWED_EXTRA = {("compiler/plan.py", "_columns")}
SHARED = {
    "columns_read": "model/expressions.py",
    "dimension_columns": "model/expressions.py",
    "dependency_closure": "model/metrics.py",
    "direct_inputs": "model/metrics.py",
    "metric_unit": "model/metrics.py",
}


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(SRC)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(SRC.rglob("*.py"))
    ]


def _function_names(tree: ast.Module) -> list[str]:
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _inline_unit_lines(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "unit"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "format"
    ]


def _definitions() -> list[tuple[str, str]]:
    return [(relative, name) for relative, tree in _modules() for name in _function_names(tree)]


def test_no_removed_copy_is_defined_again() -> None:
    again = [
        f"{relative}: {name}"
        for relative, name in _definitions()
        if name in REMOVED
        and relative not in REMOVED[name]
        and (relative, name) not in ALLOWED_EXTRA
    ]
    assert again == []


def test_each_shared_function_is_defined_once_and_in_the_model() -> None:
    found = [(relative, name) for relative, name in _definitions() if name in SHARED]
    assert sorted(found) == sorted((path, name) for name, path in SHARED.items())


def test_the_unit_of_a_metric_is_not_written_inline() -> None:
    inline = [
        f"{relative}:{line}"
        for relative, tree in _modules()
        if relative != "model/metrics.py"
        for line in _inline_unit_lines(tree)
    ]
    assert inline == []


def test_the_scan_sees_a_copy() -> None:
    """The scan itself works: it finds a removed name and an inline unit in a bad module."""
    tree = ast.parse("def _closure(m):\n    return m.format.unit\n")
    assert _function_names(tree) == ["_closure"] and "_closure" in REMOVED
    assert _inline_unit_lines(tree) == [2]


# --- the helpers behave as the copies did -------------------------------------------------


def _metric(name: str, **fields: object) -> Metric:
    base: dict[str, object] = {
        "apiVersion": "makna/v1",
        "kind": "metric",
        "name": name,
        "label": name.title(),
        "description": f"The {name} metric of the test domain, written for a reader.",
        "owner": {"team": "test"},
        "status": "approved",
        "sensitivity": "internal",
        "domain": "test",
        "synonyms": {"en": ["x"], "id": ["y"]},
        "format": {"kind": "number"},
    }
    return Metric.model_validate({**base, **fields})


def _simple(name: str) -> Metric:
    return _metric(name, type="simple", measure="thing.total")


def _ratio(name: str, numerator: str, denominator: str) -> Metric:
    return _metric(name, type="ratio", numerator=numerator, denominator=denominator)


def _derived(name: str, *inputs: str) -> Metric:
    return _metric(name, type="derived", expr=" + ".join(inputs), inputs=list(inputs))


def _by_name(*metrics: Metric) -> dict[str, Metric]:
    return {m.name: m for m in metrics}


def test_the_closure_lists_inputs_first_and_the_metric_last_each_once() -> None:
    a, b = _simple("aa"), _simple("bb")
    r = _ratio("rr", "aa", "bb")
    d = _derived("dd", "rr", "aa")
    metrics = _by_name(a, b, r, d)
    assert [m.name for m in dependency_closure(metrics, d)] == ["aa", "bb", "rr", "dd"]
    assert [m.name for m in dependency_closure(metrics, a)] == ["aa"]
    assert direct_inputs(r) == ("aa", "bb") and direct_inputs(d) == ("rr", "aa")
    assert direct_inputs(a) == ()


def test_a_missing_input_and_a_cycle_raise_unless_the_caller_asks_for_leniency() -> None:
    r = _ratio("rr", "aa", "gone")
    metrics = _by_name(_simple("aa"), r)
    with pytest.raises(MetricDependencyError, match="gone, which is not a metric") as missing:
        dependency_closure(metrics, r)
    assert missing.value.metric == "rr"
    assert [m.name for m in dependency_closure(metrics, r, strict=False)] == ["aa", "rr"]

    x, y = _derived("xx", "yy"), _derived("yy", "xx")
    loop = _by_name(x, y)
    with pytest.raises(MetricDependencyError, match="depends on itself"):
        dependency_closure(loop, x)
    assert [m.name for m in dependency_closure(loop, x, strict=False)] == ["yy", "xx"]


def test_the_unit_is_the_declared_one_else_percent_for_a_percent_format() -> None:
    assert (
        metric_unit(_metric("aa", type="simple", measure="tt.mm", format={"kind": "number"}))
        is None
    )
    assert metric_unit(
        _metric("bb", type="simple", measure="tt.mm", format={"kind": "percent"})
    ) == ("percent")
    declared = _metric(
        "cc", type="simple", measure="tt.mm", format={"kind": "currency", "unit": "IDR"}
    )
    assert metric_unit(declared) == "IDR"
