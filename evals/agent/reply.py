"""The final reply of an agent run (docs/EVALS.md section 5.2).

The agent ends with one JSON object:

    {"status": "answered | needs_clarification | no_governed_answer | refused",
     "answer": {"columns": ["region", "value"], "rows": [["Jawa Barat", 0.031]]},
     "interpretation": "...", "sql": "..."}

What `parse_reply` accepts: the **last top-level JSON object** found in the reply text. The object
may stand alone, sit in a Markdown code fence, or follow prose; earlier objects (and braces in the
prose) are ignored. Top level means not inside an array or another object: `[{"status":
"refused"}]` has no reply, nor does an object inside an outer object that never closes; and a
reply that ends inside an unclosed `{` or `[` is truncated, so it has none. Only the last 1,000,000
characters are read, and a nesting too deep for the decoder is unparseable, not a crash. Nothing
is repaired: a truncated object, a JSON array, a second object that lacks `status`, `NaN` or
`Infinity`, a `status` that is not one of the four values, an `answer` whose rows do not fit its
columns, or an `interpretation` or `sql` that is not text all make the reply unparseable, and an
unparseable reply scores wrong (section 5.3). The reply is only read as JSON (`json.JSONDecoder`),
never evaluated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_TEXT = 1_000_000
"""Characters of a reply that are read (its end); a longer reply is not a reply to this format."""
MAX_SPANS = 100
STATUSES = ("answered", "needs_clarification", "no_governed_answer", "refused")


@dataclass(frozen=True)
class Reply:
    status: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    has_answer: bool = False
    """True when the object carried an `answer` (even one with no rows)."""
    interpretation: str = ""
    sql: str = ""


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


_OPEN = re.compile(r"[{\[]")
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]')


def top_level_spans(text: str) -> tuple[list[tuple[int, int]], bool]:
    """`(spans, closed)`: the (start, end) of each balanced top-level `{...}` of `text`, and
    whether every top-level `{` or `[` was closed. Brackets are counted outside strings once
    inside a bracket; quotes in the prose around the reply are ignored. Linear in the text."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    is_object = False
    position = 0
    while True:
        if depth == 0:
            found = _OPEN.search(text, position)
            if found is None:
                return spans, True
            start, is_object, depth = found.start(), found.group() == "{", 1
        else:
            found = _TOKEN.search(text, position)
            if found is None:
                return spans, False
            token = found.group()
            if token in "{[":
                depth += 1
            elif token in "}]":
                depth -= 1
                if depth == 0 and is_object:
                    spans.append((start, found.end()))
        position = found.end()


def last_object(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object of `text`, or None.

    Only the last `MAX_TEXT` characters are read. Top level means not inside another object or an
    array: `[{"status": "refused"}]` holds no reply, and neither does an object inside an outer
    object that never closes. A reply that ends inside an unclosed `{` or `[` is truncated, so it
    has no reply. Of the last `MAX_SPANS` balanced objects the last one that is valid JSON wins;
    a nesting too deep to decode is invalid.
    """
    text = text[-MAX_TEXT:]
    spans, closed = top_level_spans(text)
    if not closed:
        return None
    for start, end in reversed(spans[-MAX_SPANS:]):
        try:
            value = _DECODER.decode(text[start:end])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict):
            return value
    return None


def parse_reply(text: str) -> Reply | None:
    """The reply the agent gave, or None when it does not parse (and so scores wrong)."""
    data = last_object(text)
    if data is None:
        return None
    status = data.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        return None
    texts: dict[str, str] = {}
    for name in ("interpretation", "sql"):
        value = data.get(name)
        if value is None:
            texts[name] = ""
        elif isinstance(value, str):
            texts[name] = value
        else:
            return None
    answer = data.get("answer")
    if answer is None:
        if status == "answered":
            return None
        return Reply(status, interpretation=texts["interpretation"], sql=texts["sql"])
    if not isinstance(answer, dict):
        return None
    columns, rows = answer.get("columns"), answer.get("rows")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        return None
    if not isinstance(rows, list):
        return None
    checked: list[tuple[Any, ...]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return None
        if any(isinstance(cell, dict | list) for cell in row):
            return None
        checked.append(tuple(row))
    return Reply(
        status,
        tuple(columns),
        tuple(checked),
        True,
        texts["interpretation"],
        texts["sql"],
    )
