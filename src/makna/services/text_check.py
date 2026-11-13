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
