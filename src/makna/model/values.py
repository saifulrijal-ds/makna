"""Which filter values fit a dimension type (V10 and the planner's INVALID_FILTER).

One answer for the whole code base. A time value is written `YYYY-MM-DD` or `YYYY-MM-DD HH:MM[:SS]`
and nothing else: Python's ISO parser also reads week dates (`2026-W01-1`), a `T` separator, and
a bare hour, and PostgreSQL's `CAST(... AS DATE)` or `CAST(... AS TIMESTAMP)` rejects several of
them, so a request would compile to SQL that fails on the engine (ARCHITECTURE section 8.4).
"""

from __future__ import annotations

import datetime
import math
import re

TIME_VALUE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}( [0-9]{2}:[0-9]{2}(:[0-9]{2})?)?")
"""The two spellings of a time value; ASCII digits only."""


def is_time_value(value: object) -> bool:
    """True for a real calendar date or timestamp written in one of the two spellings."""
    if not isinstance(value, str) or not TIME_VALUE.fullmatch(value):
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_date_only(value: str) -> bool:
    """True if `value` is a time value that is a date (`YYYY-MM-DD`), not a timestamp."""
    return len(value) == 10 and is_time_value(value)


def value_fits_dimension(dimension_type: str, value: object) -> bool:
    """True if `value` can be a filter value for a dimension of `dimension_type`: a boolean for
    `boolean`, a finite number for `numeric`, a time value for `time`, and for `categorical` a
    string or a finite number (a boolean is never a value of a categorical dimension)."""
    if dimension_type == "boolean":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if dimension_type == "time":
        return is_time_value(value)
    if isinstance(value, str):
        return dimension_type != "numeric"
    return isinstance(value, (int, float)) and math.isfinite(value)


_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")
_NUMBER_TEXT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_BOOLEAN_TEXT = frozenset({"true", "false", "t", "f", "yes", "no", "y", "n", "on", "off", "1", "0"})
_TEXT_TYPES = frozenset({"string"})
_NUMBER_TYPES = frozenset({"integer", "decimal", "float"})
_TIME_TYPES = frozenset({"date", "timestamp"})


def value_fits_column(dimension_type: str, value: object, data_type: str) -> bool:
    """True unless PostgreSQL would reject the comparison of the literal the compiler writes for
    `value` with a column that the catalog types as `data_type` (C-9). The planner does not know
    column types; the service does (D-30), and calls this for a dimension that reads one column.

    The literal is a typed constant, except text: a time dimension writes `CAST(... AS DATE)` or
    `AS TIMESTAMP`, a boolean value `TRUE`/`FALSE`, a number a numeric constant, and a string an
    untyped text constant that PostgreSQL reads as the type of the column (so it must be written
    the way that type is read). A column of type `other` is never refused.
    """
    if data_type == "other" or data_type not in (
        _TEXT_TYPES | _NUMBER_TYPES | _TIME_TYPES | {"boolean"}
    ):
        return True
    if dimension_type == "time":
        return data_type in _TIME_TYPES
    if isinstance(value, bool):
        return data_type == "boolean"
    if isinstance(value, (int, float)):
        return data_type in _NUMBER_TYPES
    if not isinstance(value, str):
        return False
    if data_type in _TEXT_TYPES:
        return True
    if data_type == "integer":
        return bool(_INTEGER_TEXT.fullmatch(value))
    if data_type in _NUMBER_TYPES:
        return bool(_NUMBER_TEXT.fullmatch(value))
    if data_type == "boolean":
        return value.lower() in _BOOLEAN_TEXT
    return is_time_value(value)
