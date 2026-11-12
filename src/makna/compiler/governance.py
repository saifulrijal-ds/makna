"""Governance of personal data (ARCHITECTURE section 7.6, QRY-2): masks, and the final check.

`mask_expression` builds the SQL node that replaces a personal dimension's value. `verify` walks
the finished syntax tree and fails if any column read by a personal dimension appears anywhere
except inside a mask or inside `COUNT(...)` or `COUNT(DISTINCT ...)`. The check catches mistakes
in the generator itself; it is not a second planner and does not look at the model.

All SQL is built as sqlglot nodes. No text is formatted or concatenated.
"""

from __future__ import annotations

from sqlglot import exp

from makna.model.definitions import MaskBucket, MaskRedact, MaskTruncateDate

# The result column that says how many groups the minimum group size left out. Its name starts
# with two underscores, which no entity, dimension, or metric name can (names start with a
# letter), so it cannot clash with a `<entity>__<dimension>` or a metric column.
GROUPS_LEFT_OUT = "__groups_left_out"
# Prefix of the per-entity record counts inside the generated SQL (never returned).
RECORDS_PREFIX = "__records_"

REDACTED = "***"


def mask_expression(
    masking: MaskRedact | MaskTruncateDate | MaskBucket, value: exp.Expr
) -> exp.Expr:
    """The expression that replaces `value` for a personal dimension.

    - `redact`: the constant `***`, typed (PostgreSQL rejects a bare string constant in GROUP BY).
    - `truncate_date`: the first day of the month, quarter, or year, as a date.
    - `bucket`: the lower bound of the band of the given width. The value is cast to a floating
      type first, because `a / b` on two integers truncates on PostgreSQL and not on DuckDB, and
      truncation toward zero would give the wrong band for negative values.
    """
    if isinstance(masking, MaskRedact):
        return exp.Cast(this=exp.Literal.string(REDACTED), to=exp.DataType.build("text"))
    if isinstance(masking, MaskTruncateDate):
        return exp.Cast(
            this=exp.TimestampTrunc(this=value, unit=exp.Literal.string(masking.grain)),
            to=exp.DataType.build("date"),
        )
    width = _number(masking.width)
    banded = exp.Floor(
        this=exp.Div(
            this=exp.Cast(this=value, to=exp.DataType.build("double")), expression=width.copy()
        )
    )
    return exp.Mul(this=banded, expression=width)


def _number(value: float) -> exp.Literal:
    return exp.Literal.number(int(value) if float(value).is_integer() else value)
