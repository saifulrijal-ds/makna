"""What a metric depends on, and the unit it is shown in.

One answer each for the whole code base (the planner, the services). They live in `makna.model`
because every package may import it (the dependency rule of ARCHITECTURE section 2).
"""

from __future__ import annotations

from collections.abc import Mapping

from makna.model.definitions import Metric


class MetricDependencyError(ValueError):
    """A metric depends on itself, or on a name that is not a metric."""

    def __init__(self, metric: str, message: str) -> None:
        super().__init__(message)
        self.metric = metric
        self.message = message


def direct_inputs(metric: Metric) -> tuple[str, ...]:
    """The names a metric is computed from: numerator and denominator of a ratio, the inputs of
    a derived metric, nothing for a simple one."""
    if metric.type == "ratio":
        return tuple(n for n in (metric.numerator, metric.denominator) if n is not None)
    if metric.type == "derived":
        return tuple(metric.inputs or ())
    return ()


def dependency_closure(
    metrics: Mapping[str, Metric], root: Metric, *, strict: bool = True
) -> list[Metric]:
    """The metric and everything it depends on, each once, inputs first, `root` last. The order
    is stable: depth first, inputs in the order the definition lists them.

    A cycle or an input that is not in `metrics` raises `MetricDependencyError` (the metric that
    cannot be expanded is `.metric`, the reason `.message`). With `strict=False` such an input is
    left out and a cycle is cut, for callers that describe a metric whose inputs may be broken.
    """
    order: list[Metric] = []
    done: set[str] = set()

    def visit(metric: Metric, trail: tuple[str, ...]) -> None:
        if metric.name in done:
            return
        if metric.name in trail:
            if strict:
                raise MetricDependencyError(root.name, f"metric {metric.name} depends on itself")
            return
        for name in direct_inputs(metric):
            child = metrics.get(name)
            if child is None:
                if strict:
                    raise MetricDependencyError(
                        root.name, f"{metric.name} uses {name}, which is not a metric"
                    )
                continue
            visit(child, (*trail, metric.name))
        done.add(metric.name)
        order.append(metric)

    visit(root, ())
    return order


def metric_unit(metric: Metric) -> str | None:
    """The unit of a metric's value: its declared unit, else `percent` for a percent format."""
    return metric.format.unit or ("percent" if metric.format.kind == "percent" else None)
