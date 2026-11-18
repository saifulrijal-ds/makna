"""The final reply of an agent run (docs/EVALS.md section 5.2).

The agent ends with one JSON object:

    {"status": "answered | needs_clarification | no_governed_answer | refused",
     "answer": {"columns": ["region", "value"], "rows": [["Jawa Barat", 0.031]]},
     "interpretation": "...", "sql": "..."}

What `parse_reply` accepts: the **last top-level JSON object** found in the reply text. The object
may stand alone, sit in a Markdown code fence, or follow prose; earlier objects (and braces in the
prose) are ignored. Nothing is repaired: a truncated object, a JSON array, a second object that
lacks `status`, `NaN` or `Infinity`, a `status` that is not one of the four values, an `answer`
whose rows do not fit its columns, or an `interpretation` or `sql` that is not text all make the
reply unparseable, and an unparseable reply scores wrong (section 5.3). The reply is only read as
JSON (`json.JSONDecoder`), never evaluated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

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


def last_object(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object of `text`, or None."""
    found: dict[str, Any] | None = None
    position = text.find("{")
    while position != -1:
        try:
            value, end = _DECODER.raw_decode(text, position)
        except ValueError:
            position = text.find("{", position + 1)
            continue
        if isinstance(value, dict):
            found = value
        position = text.find("{", end)
    return found


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
