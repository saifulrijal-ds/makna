"""The line-by-line comparison of two definition texts, shared by the editor (WP-19) and the
review pages (WP-19B).

The rows are plain strings that a template escapes: nothing here builds HTML, and the standard
library's `difflib.HtmlDiff` is not used. Both texts are the canonical form of a proposal (status
`draft`, the official version number), so what Makna sets itself is not a change.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

MAX_DIFF_LINES = 3000
"""A text longer than this (either side) is not compared: the comparison is quadratic in the
worst case, and a page of thousands of rows helps nobody."""
DIFF_CONTEXT = 3
"""Unchanged lines shown around a change."""


@dataclass(frozen=True)
class DiffRow:
    """One row of the side-by-side view. `kind` is `same`, `changed`, `removed`, `added`, or
    `skipped` (a run of unchanged lines, `skipped` of them, left out)."""

    kind: str
    left_no: int | None = None
    left: str = ""
    right_no: int | None = None
    right: str = ""
    skipped: int = 0


def build_diff(official: str, proposed: str) -> list[DiffRow] | None:
    """The rows that compare two texts line by line, or None when they are too long to compare.
    Plain strings only: the template escapes them."""
    a, b = official.splitlines(), proposed.splitlines()
    if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
        return None
    rows: list[DiffRow] = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows += [
                DiffRow("same", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k])
                for k in range(i2 - i1)
            ]
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            has_left, has_right = i1 + k < i2, j1 + k < j2
            kind = "changed" if has_left and has_right else "removed" if has_left else "added"
            rows.append(
                DiffRow(
                    kind,
                    i1 + k + 1 if has_left else None,
                    a[i1 + k] if has_left else "",
                    j1 + k + 1 if has_right else None,
                    b[j1 + k] if has_right else "",
                )
            )
    return _collapse(rows)


def count_changes(official: str, proposed: str) -> tuple[int, int] | None:
    """`(lines added, lines removed)` between two texts, from the same comparison as
    `build_diff`, or None when they are too long to compare. A line that changed counts as one
    removed and one added."""
    a, b = official.splitlines(), proposed.splitlines()
    if len(a) > MAX_DIFF_LINES or len(b) > MAX_DIFF_LINES:
        return None
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    return added, removed


def _collapse(rows: list[DiffRow]) -> list[DiffRow]:
    """Keep `DIFF_CONTEXT` unchanged lines around each change; a longer run of unchanged lines
    becomes one `skipped` row."""
    changed = [i for i, row in enumerate(rows) if row.kind != "same"]
    if not changed:
        return []
    keep = [False] * len(rows)
    for index in changed:
        for near in range(max(0, index - DIFF_CONTEXT), min(len(rows), index + DIFF_CONTEXT + 1)):
            keep[near] = True
    out: list[DiffRow] = []
    run = 0
    for row, shown in zip(rows, keep, strict=True):
        if shown:
            if run:
                out.append(DiffRow("skipped", skipped=run))
                run = 0
            out.append(row)
        else:
            run += 1
    if run:
        out.append(DiffRow("skipped", skipped=run))
    return out
