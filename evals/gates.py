"""Gates: read `evals/gates.toml`, check a report against them, and decide the exit code.

A gate that cannot be computed (no questions of its kind, a section that did not run when it
should have) fails. A gate of a section that this run did not cover (`retrieval` only, `compile`
only) is not evaluated.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATES_FILE = Path(__file__).resolve().parent / "gates.toml"
EPSILON = 1e-9

Getter = Callable[[Mapping[str, Any]], float | None]


class GateError(Exception):
    """`gates.toml` is missing a gate, holds an unknown one, or holds a bad value."""


@dataclass(frozen=True)
class GateSpec:
    section: str
    key: str
    label: str
    op: str
    """`>=`, `<=`, or `<`: how the value must compare with the threshold."""
    read: Getter


@dataclass(frozen=True)
class GateResult:
    name: str
    label: str
    op: str
    threshold: float
    value: float | None
    passed: bool
    failures: tuple[str, ...] = field(default=())
    """For compile gates: the requests that failed, each as `id: reason`."""


def _fused(report: Mapping[str, Any]) -> Mapping[str, Any]:
    scores: Mapping[str, Any] = report["retrieval"]["scores"]
    fused: Mapping[str, Any] = scores["fused"]
    return fused


def _language_recall(report: Mapping[str, Any], language: str) -> float | None:
    value = _fused(report)["by_language"][language]["recall_at_3"]
    return None if value is None else float(value)


def _gap(report: Mapping[str, Any]) -> float | None:
    english, indonesian = _language_recall(report, "en"), _language_recall(report, "id")
    return None if english is None or indonesian is None else english - indonesian


def _score(name: str) -> Getter:
    def read(report: Mapping[str, Any]) -> float | None:
        value = _fused(report)[name]
        return None if value is None else float(value)

    return read


def _heldout_recall(report: Mapping[str, Any]) -> float | None:
    heldout = report["retrieval"].get("heldout")
    value = None if heldout is None else heldout["scores"]["fused"]["recall_at_3"]
    return None if value is None else float(value)


def _latency(report: Mapping[str, Any]) -> float | None:
    value = report["retrieval"]["latency"]["p95_ms"]
    return None if value is None else float(value)


def _part(name: str) -> Getter:
    def read(report: Mapping[str, Any]) -> float | None:
        part = report["compile"][name]
        return float(part["pass_rate"]) if part["total"] else None

    return read


SPECS: tuple[GateSpec, ...] = (
    GateSpec("retrieval", "recall_at_3_min", "Recall at 3 (fused)", ">=", _score("recall_at_3")),
    GateSpec("retrieval", "mrr_min", "Mean reciprocal rank (fused)", ">=", _score("mrr")),
    GateSpec(
        "retrieval",
        "false_abstention_rate_max",
        "False abstention rate",
        "<=",
        _score("false_abstention_rate"),
    ),
    GateSpec(
        "retrieval",
        "indonesian_recall_gap_max",
        "English minus Indonesian recall at 3",
        "<=",
        _gap,
    ),
    GateSpec(
        "retrieval",
        "heldout_recall_at_3_min",
        "Held-out recall at 3 (fused)",
        ">=",
        _heldout_recall,
    ),
    GateSpec("retrieval", "latency_p95_ms_below", "Search latency p95 (ms)", "<", _latency),
    GateSpec(
        "compile", "part_a_pass_rate_min", "Compile part A (golden requests)", ">=", _part("part_a")
    ),
    GateSpec(
        "compile",
        "part_b_pass_rate_min",
        "Compile part B (every approved metric)",
        ">=",
        _part("part_b"),
    ),
    GateSpec("compile", "part_c_pass_rate_min", "Compile part C (refusals)", ">=", _part("part_c")),
)


def load_gates(path: Path = GATES_FILE) -> dict[str, dict[str, float]]:
    """The thresholds by section. Raises `GateError` for a missing or unknown gate."""
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise GateError(f"{path}: cannot read the gates: {error}") from error
    expected: dict[str, set[str]] = {}
    for spec in SPECS:
        expected.setdefault(spec.section, set()).add(spec.key)
    problems: list[str] = []
    for section, keys in expected.items():
        found = raw.get(section)
        if not isinstance(found, dict):
            problems.append(f"missing section [{section}]")
            continue
        for key in sorted(keys - set(found)):
            problems.append(f"[{section}] is missing {key}")
        for key in sorted(set(found) - keys):
            problems.append(f"[{section}] has an unknown gate {key}")
        for key, value in found.items():
            if isinstance(value, bool) or not isinstance(value, int | float):
                problems.append(f"[{section}] {key} must be a number")
    for section in sorted(set(raw) - set(expected)):
        problems.append(f"unknown section [{section}]")
    if problems:
        raise GateError(f"{path}: " + "; ".join(problems))
    return {s: {k: float(v) for k, v in raw[s].items()} for s in expected}


def _passes(op: str, value: float, threshold: float) -> bool:
    if op == ">=":
        return value >= threshold - EPSILON
    if op == "<=":
        return value <= threshold + EPSILON
    return value < threshold


def _failed_requests(report: Mapping[str, Any], spec: GateSpec) -> tuple[str, ...]:
    if spec.section != "compile":
        return ()
    part = report["compile"][spec.key.removesuffix("_pass_rate_min")]
    return tuple(f"{item['id']}: {item['reason']}" for item in part["failures"])


def evaluate(
    report: Mapping[str, Any], gates: Mapping[str, Mapping[str, float]]
) -> list[GateResult]:
    """Check the sections present in `report` against `gates`."""
    results: list[GateResult] = []
    for spec in SPECS:
        if spec.section not in report:
            continue
        threshold = gates[spec.section][spec.key]
        value = spec.read(report)
        passed = value is not None and _passes(spec.op, value, threshold)
        results.append(
            GateResult(
                name=f"{spec.section}.{spec.key}",
                label=spec.label,
                op=spec.op,
                threshold=threshold,
                value=value,
                passed=passed,
                failures=() if passed else _failed_requests(report, spec),
            )
        )
    return results


def exit_code(results: list[GateResult]) -> int:
    """0 when every evaluated gate passed, else 1."""
    return 0 if all(r.passed for r in results) else 1
