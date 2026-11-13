"""Removing personal data that can be detected, before anything is stored (D-24, GOV-2).

`redact` is a pure function over text. It replaces, with `[REDACTED]`:

- email addresses (any word with an `@` in it, so partial and odd forms go too);
- phone-like numbers: international forms with a `+` (at least seven digits, with spaces, dots,
  dashes or brackets), and Indonesian forms starting with `0` (mobile and landline, with an
  optional bracketed area code and spaces, dots or dashes);
- any run of 9 or more digits, where one or two spaces, dots or dashes between digits do not end
  the run (`4111 1111 1111 1111`, `3201-0101-9001-0001`).

A name typed into free text cannot be detected, so the tool-call log stays visible to approvers
and admins only (D-24). `clip` cuts text to a length without leaving the start of a number at
the cut.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_EMAIL = re.compile(r"\S*@\S*")
# A plus sign, then at least seven digits with the usual separators.
_INTERNATIONAL = re.compile(r"\+\s?\d(?:[\s.\-()]{0,2}\d){6,}")
# 0, an area or operator code of one to three digits (bracketed or not), then two groups.
_INDONESIAN = re.compile(r"(?<![\d])\(?0\d{1,3}\)?[\s.\-]?\d{3,4}(?:[\s.\-]?\d{2,5})?(?![\d])")
_DIGIT_RUN = re.compile(r"\d(?:[\s.\-]{0,2}\d){8,}")
_TAIL = re.compile(r"[\d\s.\-+()]+$")

_PATTERNS = (_EMAIL, _INTERNATIONAL, _DIGIT_RUN, _INDONESIAN)


def redact(text: str) -> str:
    """`text` with emails, phone-like numbers, and runs of 9 or more digits replaced."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def clip(text: str, limit: int) -> str:
    """At most `limit` characters of `text`. When it is cut, the digits, separators and signs at
    the cut are dropped too, so that the first digits of a long number are not left behind."""
    if len(text) <= limit:
        return text
    return _TAIL.sub("", text[:limit])


def redact_value(value: Any, limit: int = 500) -> Any:
    """`redact` applied to every string in a JSON-like value (strings are clipped first).

    Numbers, booleans and None are kept, except that a whole number of 9 or more digits becomes
    `[REDACTED]`. Anything else (an object of another type) becomes `[REDACTED]`.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return redact(clip(value, limit))
    if isinstance(value, int):
        return REDACTED if len(str(abs(value))) >= 9 else value
    if isinstance(value, float):
        return REDACTED if len(str(abs(int(value)))) >= 9 else value
    if isinstance(value, (list, tuple)):
        return [redact_value(item, limit) for item in value[:100]]
    if isinstance(value, dict):
        return {
            redact(clip(str(key), 100)): redact_value(item, limit)
            for key, item in list(value.items())[:100]
        }
    return REDACTED
