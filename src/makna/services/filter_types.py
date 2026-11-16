"""Filter values against the catalog's column types (C-9, ARCHITECTURE section 8.4).

Section 8.4 promises that a wrong request fails with an actionable error instead of SQL the
engine rejects. The planner cannot keep that promise for the type of a column: it plans against
the definitions (`Model`), and a definition does not say that `customer.province` is text and not
a number. The catalog does (`catalog_column.data_type`), and the service has it, because compiling
needs a synced catalog (D-30). So the check lives here, between planning and SQL generation, and
the compile evals pass through it because they call `compile_on_snapshot`.

It applies to a dimension that reads exactly one column and nothing else (no `expr`, or an `expr`
that is just that column). For any other expression the type of the result is not the type of a
column, and PostgreSQL's reading of the comparison is not known here, so no error is raised.
"""

from __future__ import annotations

from collections.abc import Mapping

from makna.compiler.plan import PlannedFilter, QueryPlan, filter_refusal
from makna.compiler.snapshot import Model
from makna.model.definitions import Scalar
from makna.model.expressions import bare_column
from makna.model.request import QueryError
from makna.model.values import value_fits_column


def check_filter_types(
    model: Model, plan: QueryPlan, column_types: Mapping[tuple[str, str], str]
) -> None:
    """Raise `INVALID_FILTER` for the first request filter whose value cannot be compared with the
    column its dimension reads, according to the catalog. `column_types` maps `(engine_fqn,
    column)`, lower case, to the catalog's `data_type`."""
    for flt in plan.filters:
        data_type = _column_type(model, flt, column_types)
        if data_type is None:
            continue
        values: tuple[Scalar, ...] = (
            flt.value if isinstance(flt.value, tuple) else () if flt.value is None else (flt.value,)
        )
        for value in values:
            if not value_fits_column(flt.dimension.type, value, data_type):
                raise _mismatch(flt, value, data_type)


def _column_type(
    model: Model, flt: PlannedFilter, column_types: Mapping[tuple[str, str], str]
) -> str | None:
    column = bare_column(flt.dimension.expr) if flt.dimension.expr else flt.dimension.name.lower()
    if column is None:
        return None
    table = model.entities[flt.entity].source.table.lower()
    return column_types.get((table, column))


def _mismatch(flt: PlannedFilter, value: Scalar, data_type: str) -> QueryError:
    personal = flt.sensitivity == "personal"
    shown = "the value" if personal else f"value {value!r}"
    error = filter_refusal(
        flt.ref,
        flt.dimension,
        f"{shown} cannot be compared with the column this dimension reads, which the catalog "
        f"types as {data_type}. Write the value as {_write_as(data_type)}",
        personal,
    )
    error.details["catalog_type"] = data_type
    return error


def _write_as(data_type: str) -> str:
    return {
        "string": "text",
        "integer": "a whole number",
        "decimal": "a number",
        "float": "a number",
        "boolean": "true or false",
        "date": "a date, YYYY-MM-DD",
        "timestamp": "a date, YYYY-MM-DD, or a time, YYYY-MM-DD HH:MM:SS",
    }.get(data_type, "a value of that type")
