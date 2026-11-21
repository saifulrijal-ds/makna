"""The line-by-line comparison of two definition texts, shared by the editor (WP-19) and the
review pages (WP-19B).

The rows are plain strings that a template escapes: nothing here builds HTML, and the standard
library's `difflib.HtmlDiff` is not used. Both texts are the canonical form of a proposal (status
`draft`, the official version number), so what Makna sets itself is not a change.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

MAX_MATCH_CELLS = 250_000
"""The most `lines on the left x lines on the right` that the line matching is run on, after the
common start and end are cut off. `difflib` with `autojunk=False` grows roughly with the cube of
the line count when many lines repeat (as the lines of a YAML list of dimensions do), so the
matching is bounded by this product, not by the length of the texts: 500 x 500 lines of the
worst shape take well under half a second. Above it the middle is shown as one removed block
beside one added block, so every line is still on the page, only not paired."""
DIFF_CONTEXT = 3
"""Unchanged lines shown around a change."""
MAX_PARTS_ROWS = 300
"""Changed rows that get their characters compared, and `MAX_PARTS_LINE` the longest line that
is compared (the comparison is quadratic in the length of the line)."""
MAX_PARTS_LINE = 300

Opcode = tuple[str, int, int, int, int]


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
    left_parts: tuple[tuple[str, bool], ...] = ()
    right_parts: tuple[tuple[str, bool], ...] = ()
    """For a `changed` row: each side cut into `(text, differs)` pieces, so that a page can
    highlight the characters that changed inside the line (a letter swapped for a lookalike).
    Empty for the other rows, and for lines too long to compare character by character."""


def _opcodes(a: list[str], b: list[str]) -> list[Opcode]:
    """`difflib`-style opcodes `(tag, i1, i2, j1, j2)` of two lists of lines. The common start and
    end are cut off first (linear); the matching runs on the middle only when it is small
    (`MAX_MATCH_CELLS`), else the middle is one `replace` block (or one `delete` or `insert`)."""
    start = 0
    limit = min(len(a), len(b))
    while start < limit and a[start] == b[start]:
        start += 1
    end = 0
    while end < limit - start and a[len(a) - 1 - end] == b[len(b) - 1 - end]:
        end += 1
    ops: list[Opcode] = []
    if start:
        ops.append(("equal", 0, start, 0, start))
    i2, j2 = len(a) - end, len(b) - end
    if start < i2 or start < j2:
        if (i2 - start) * (j2 - start) <= MAX_MATCH_CELLS:
            matcher = difflib.SequenceMatcher(None, a[start:i2], b[start:j2], autojunk=False)
            for tag, x1, x2, y1, y2 in matcher.get_opcodes():
                ops.append((tag, x1 + start, x2 + start, y1 + start, y2 + start))
        else:
            tag = "replace" if start < i2 and start < j2 else "delete" if start < i2 else "insert"
            ops.append((tag, start, i2, start, j2))
    if end:
        ops.append(("equal", i2, len(a), j2, len(b)))
    return ops


def build_diff(official: str, proposed: str) -> list[DiffRow]:
    """The rows that compare two texts line by line. Every line of both texts is in a row that
    is `changed`, `removed` or `added`, or in an unchanged run (`same`, collapsed to `skipped`
    when it is long). Plain strings only: the template escapes them."""
    a, b = official.splitlines(), proposed.splitlines()
    rows: list[DiffRow] = []
    detailed = 0
    for tag, i1, i2, j1, j2 in _opcodes(a, b):
        if tag == "equal":
            rows += [
                DiffRow("same", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k])
                for k in range(i2 - i1)
            ]
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            has_left, has_right = i1 + k < i2, j1 + k < j2
            kind = "changed" if has_left and has_right else "removed" if has_left else "added"
            left = a[i1 + k] if has_left else ""
            right = b[j1 + k] if has_right else ""
            left_parts: tuple[tuple[str, bool], ...] = ()
            right_parts: tuple[tuple[str, bool], ...] = ()
            if kind == "changed" and detailed < MAX_PARTS_ROWS:
                detailed += 1
                left_parts, right_parts = _parts(left, right)
            rows.append(
                DiffRow(
                    kind,
                    i1 + k + 1 if has_left else None,
                    left,
                    j1 + k + 1 if has_right else None,
                    right,
                    0,
                    left_parts,
                    right_parts,
                )
            )
    return _collapse(rows)


def _parts(
    left: str, right: str
) -> tuple[tuple[tuple[str, bool], ...], tuple[tuple[str, bool], ...]]:
    """Each line cut into `(text, differs)` pieces by a character comparison, or two empty tuples
    for a line that is too long to compare."""
    if len(left) > MAX_PARTS_LINE or len(right) > MAX_PARTS_LINE:
        return (), ()
    left_out: list[tuple[str, bool]] = []
    right_out: list[tuple[str, bool]] = []
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        same = tag == "equal"
        if i2 > i1:
            left_out.append((left[i1:i2], not same))
        if j2 > j1:
            right_out.append((right[j1:j2], not same))
    return tuple(left_out), tuple(right_out)


def count_changes(official: str, proposed: str) -> tuple[int, int]:
    """`(lines added, lines removed)` between two texts, from the same comparison as
    `build_diff`. A line that changed counts as one removed and one added."""
    added = removed = 0
    for tag, i1, i2, j1, j2 in _opcodes(official.splitlines(), proposed.splitlines()):
        if tag != "equal":
            removed += i2 - i1
            added += j2 - j1
    return added, removed


def count_rows(rows: list[DiffRow]) -> tuple[int, int]:
    """`(lines added, lines removed)` of rows that `build_diff` made, so that a page that has
    the rows does not compare the texts again."""
    added = sum(1 for r in rows if r.kind in ("changed", "added"))
    removed = sum(1 for r in rows if r.kind in ("changed", "removed"))
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
