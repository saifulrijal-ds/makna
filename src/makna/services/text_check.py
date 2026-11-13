"""Text that is refused at the boundary of every service (INVALID_INPUT).

A NUL or another control character, and an unpaired surrogate, are not text a person types, and
the JSON column of the tool-call log cannot hold some of them. Ordinary whitespace (space, tab,
newline, carriage return) is allowed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from makna.services.errors import invalid_input

_ALLOWED_CONTROLS = frozenset("\t\n\r")


def has_bad_character(text: str) -> bool:
    for char in text:
        code = ord(char)
        if char in _ALLOWED_CONTROLS:
            continue
        if code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF:
            return True
    return False


def check_text(field: str, value: object) -> None:
    """Raise `INVALID_INPUT` naming `field` when `value` is a string with a bad character."""
    if isinstance(value, str) and has_bad_character(value):
        raise invalid_input(field, "contains a control character or an invalid character")


def check_deep(field: str, value: Any) -> None:
    """`check_text` for every string (keys included) in a nested request; the field names the
    path, for example `filters.0.value`."""
    if isinstance(value, str):
        check_text(field or "request", value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{field}.{key}" if field else str(key)
            if isinstance(key, str) and has_bad_character(key):
                raise invalid_input(field or "request", "contains a key with an invalid character")
            check_deep(where, item)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            check_deep(f"{field}.{index}" if field else str(index), item)


MAX_DIMENSIONS = 20
MAX_FILTERS = 20
MAX_IN_VALUES = 100
MAX_STRING = 200


def check_request_bounds(request: Mapping[str, Any]) -> None:
    """Size bounds of a compile request (INVALID_INPUT naming the field): at most 20 dimensions,
    20 filters and 100 values in a list, and no string longer than 200 characters."""
    dimensions = request.get("dimensions")
    if isinstance(dimensions, (list, tuple)) and len(dimensions) > MAX_DIMENSIONS:
        raise invalid_input(
            "dimensions", f"at most {MAX_DIMENSIONS} dimensions", maximum=MAX_DIMENSIONS
        )
    filters = request.get("filters")
    if isinstance(filters, (list, tuple)):
        if len(filters) > MAX_FILTERS:
            raise invalid_input("filters", f"at most {MAX_FILTERS} filters", maximum=MAX_FILTERS)
        for index, item in enumerate(filters):
            value = item.get("value") if isinstance(item, Mapping) else None
            if isinstance(value, (list, tuple)) and len(value) > MAX_IN_VALUES:
                raise invalid_input(
                    f"filters.{index}.value",
                    f"at most {MAX_IN_VALUES} values",
                    maximum=MAX_IN_VALUES,
                )
    _check_lengths("", request)


def _check_lengths(field: str, value: Any) -> None:
    if isinstance(value, str):
        if len(value) > MAX_STRING:
            raise invalid_input(
                field or "request", f"at most {MAX_STRING} characters", maximum=MAX_STRING
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _check_lengths(f"{field}.{key}" if field else str(key), item)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_lengths(f"{field}.{index}" if field else str(index), item)
