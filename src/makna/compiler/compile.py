"""From a request to a response: plan it, generate the SQL, and explain it (sections 7.1 to 7.8).

These functions are pure: the same model and request give an equal response. They run no SQL.
"""

from __future__ import annotations

from makna.compiler.explain import explain
from makna.compiler.plan import QueryPlan, plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import Dialect, build_tree, to_sql
from makna.model.request import CompileResponse, MetricRef, QueryRequest


def _metric_id(name: str) -> str:
    """The `metric:<name>` reference of section 12; an identifier, not SQL."""
    return f"metric:{name}"


def compile_plan(model: Model, plan: QueryPlan, dialect: Dialect = "postgres") -> CompileResponse:
    """The SQL for a plan, printed for `dialect`, with its columns and explanation."""
    explanation = explain(model, plan)
    return CompileResponse(
        sql=to_sql(build_tree(model, plan), dialect),
        dialect=dialect,
        columns=plan.columns,
        explanation=explanation.sentences,
        warnings=explanation.warnings,
        metric=MetricRef(id=_metric_id(plan.metric), version=plan.version),
    )


def compile_request(
    model: Model,
    request: QueryRequest,
    dialect: Dialect = "postgres",
    *,
    enforce_status: bool = True,
) -> CompileResponse:
    """Plan `request` and compile the plan, or raise one `QueryError`."""
    plan = plan_request(model, request, enforce_status=enforce_status)
    return compile_plan(model, plan, dialect)
