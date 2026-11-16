"""Which columns a definition expression reads.

This is the one answer to that question for the whole code base (the planner, the final check,
the validator, the reference check, the indexer). It lives in `makna.model` because every package
may import it (the dependency rule of ARCHITECTURE section 2), and it uses sqlglot, which the
model package already uses for its function allow-list.

An expression that does not parse raises `ExpressionError`. It never answers "no columns" and
never answers a guess: a column that might be personal must not be treated as not read, and a
check must not pass because the parse failed. Each caller turns the error into its own refusal:
the validator reports an issue, the reference check reports a finding, the planner and the final
check refuse the request, and the sensitivity level of the dimension becomes the strictest one.
"""

from __future__ import annotations

from functools import lru_cache

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from makna.model.definitions import Dimension


class ExpressionError(ValueError):
    """A definition expression does not parse as one SQL expression, so the columns it reads
    cannot be known. `where` is (entity, dimension) when a caller that knows them has set it."""

    where: tuple[str, ...] = ()


@lru_cache(maxsize=4096)
def _read(sql: str) -> tuple[str, ...]:
    # The message never quotes the text: it reaches agents (the planner's refusal), and an
    # expression can hold a table's column names. Curators get the detail from V05.
    try:
        tree = sqlglot.parse_one(sql, into=exp.Condition)
    except SqlglotError:
        raise ExpressionError("does not parse as one SQL expression") from None
    if tree.find(exp.Command) is not None:
        # sqlglot gives up on some texts (one that starts with `IF`) and keeps them as raw text
        # that the printer writes out as it is. Such a node reads no columns that we can see, and
        # PostgreSQL may read some: it is not an expression we can reason about.
        raise ExpressionError("is not an expression sqlglot can read (it is kept as raw text)")
    return tuple(dict.fromkeys(c.name.lower() for c in tree.find_all(exp.Column)))


def columns_read(sql: str | None) -> tuple[str, ...]:
    """Lower-case names of the columns a SQL expression reads, each once, in order of first use.

    `None` reads nothing (a measure without a filter). Raises `ExpressionError` when the text
    does not parse.
    """
    if sql is None:
        return ()
    return _read(sql)


def dimension_columns(dimension: Dimension) -> tuple[str, ...]:
    """Lower-case names of the columns a dimension reads: the column of its own name when it has
    no `expr`, else the columns of the expression. Raises `ExpressionError` as `columns_read`."""
    if dimension.expr is None:
        return (dimension.name.lower(),)
    return _read(dimension.expr)
