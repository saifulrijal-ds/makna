"""V19: every example request of a metric plans and compiles with no error (section 5.3, D-18).

This is an `ExtraCheck` for `makna.definitions.validate.validate`. It lives in the service layer
because it needs both sides: the definitions loader (which the compiler may not import) and the
compiler (which the definitions package may not import).

Each example request goes through `makna.compiler.plan.plan_request` and then through SQL
generation for the `postgres` dialect, and the SQL must re-parse with sqlglot's `postgres`
dialect. Nothing is executed: `makna validate` needs no database. The status gate is off here,
because the check runs when a metric is submitted and approved, before it is approved.

Names resolve as `validate` resolves them: an official file wins over a proposal of the same
name, and the metric under check is always seen as itself, so a proposal's example requests are
checked against the proposal's own content. A metric whose references do not resolve is left to
the rules that report that (V07 to V09, V18); it does not get a second error here.
"""

from __future__ import annotations

from sqlglot.errors import SqlglotError

from makna.compiler.compile import compile_plan
from makna.compiler.governance import GovernanceError
from makna.compiler.plan import plan_request
from makna.compiler.snapshot import Model
from makna.compiler.sqlgen import reparse
from makna.definitions.issues import Issue
from makna.definitions.loader import DefinitionFile, DefinitionSet
from makna.model.definitions import Entity, Metric
from makna.model.request import ErrorCode, QueryError, QueryRequest


def model_from_definitions(definitions: DefinitionSet, own: Metric | None = None) -> Model:
    """A snapshot of a definitions directory: official files first, then proposals.

    `own` replaces the metric of the same name (the file being checked).
    """
    entities: list[Entity] = []
    metrics: list[Metric] = []
    for want_proposal in (False, True):
        for file in definitions.valid_files():
            if file.is_proposal != want_proposal:
                continue
            if isinstance(file.definition, Entity):
                entities.append(file.definition)
            elif isinstance(file.definition, Metric):
                metrics.append(file.definition)
    if own is not None:
        metrics.insert(0, own)
    return Model.build(entities, metrics)


def check_example_requests(definitions: DefinitionSet) -> list[Issue]:
    """V19 for every metric file of `definitions`."""
    base = model_from_definitions(definitions)
    issues: list[Issue] = []
    for file in definitions.valid_files():
        metric = file.definition
        if not isinstance(metric, Metric) or not metric.example_requests:
            continue
        is_base = base.metrics.get(metric.name) is metric
        model = base if is_base else model_from_definitions(definitions, own=metric)
        for index, example in enumerate(metric.example_requests):
            request = QueryRequest.from_example(metric.name, example.request)
            try:
                plan = plan_request(model, request, enforce_status=False)
            except QueryError as error:
                if error.code == ErrorCode.BROKEN_REFERENCE:
                    break
                issues.append(_issue(file, index, example.question, "plan", error))
                continue
            try:
                reparse(compile_plan(model, plan, "postgres").sql, "postgres")
            except QueryError as error:
                if error.code == ErrorCode.BROKEN_REFERENCE:
                    break
                issues.append(_issue(file, index, example.question, "compile to SQL", error))
            except GovernanceError as error:
                issues.append(
                    file.issue(
                        "V19",
                        f"example request {index + 1} ({example.question!r}) compiles to SQL "
                        f"that breaks a personal-data rule: {error}",
                        ("example_requests", index, "request"),
                    )
                )
            except SqlglotError as error:
                first = str(error).splitlines()[0]
                issues.append(
                    file.issue(
                        "V19",
                        f"example request {index + 1} ({example.question!r}) compiles to SQL "
                        f"that does not parse for postgres: {first}",
                        ("example_requests", index, "request"),
                    )
                )
    return issues


def _issue(file: DefinitionFile, index: int, question: str, step: str, error: QueryError) -> Issue:
    return file.issue(
        "V19",
        f"example request {index + 1} ({question!r}) does not {step}: "
        f"{error.code}: {error.message}",
        ("example_requests", index, "request"),
    )
