"""The line-by-line comparison of two definition texts, shared by the editor (WP-19) and the
review pages (WP-19B).

The rows are plain strings that a template escapes: nothing here builds HTML, and the standard
library's `difflib.HtmlDiff` is not used. Both texts are the canonical form of a proposal (status
`draft`, the official version number), so what Makna sets itself is not a change.
"""

from __future__ import annotations

import bisect
import difflib
from collections import Counter
from dataclasses import dataclass

MAX_MATCH_CELLS = 250_000
"""The most `lines on the left x lines on the right` that the line matching is run on, for one
stretch between two anchors (below). `difflib` with `autojunk=False` grows roughly with the cube
of the line count when many lines repeat (as the lines of a YAML list of dimensions do), so the
matching is bounded by this product, not by the length of the texts. Measured worst case on this
machine, for one stretch at the bound (500 x 500 lines of the worst shape, one repeated line
against an alternating pair): about 1.5 s. Above it the stretch is shown as one block, its lines
paired by position (a pair whose two lines are equal is unchanged), and the page says so."""
MAX_MATCH_TOTAL = 500_000
"""The most cells of line matching for one whole comparison, summed over all its gaps. Many gaps
just under `MAX_MATCH_CELLS`, each between two anchors, would otherwise add up (10 of them took
15.8 s). A gap that does not fit what is left of the budget is a block, with its notice. The
worst case for a whole page is then about 3 to 3.5 s of matching."""
DIFF_CONTEXT = 3
"""Unchanged lines shown around a change."""
MAX_PARTS_WORK = 500_000
"""The total of `len(left) x len(right)` over the changed lines of one page whose characters are
compared (the comparison of two lines is quadratic in their length: about 0.28 s for two lines of
300 characters of the worst shape). Past it, a changed line is shown whole, with the code point
of every non-ASCII character in it (`codepoints`, linear)."""
MAX_PARTS_LINE = 300
"""The longest line whose characters are compared."""

Opcode = tuple[str, int, int, int, int]
"""`(tag, i1, i2, j1, j2)`: `equal`, `replace`, `delete`, `insert` (as `difflib`), and `block`, a
stretch too big to match, whose lines are paired by position."""


@dataclass(frozen=True)
class DiffRow:
    """One row of the side-by-side view. `kind` is `same`, `changed`, `removed`, `added`,
    `skipped` (a run of unchanged lines, `skipped` of them, left out), or `unmatched` (a notice
    that the next `skipped` rows were paired by position, not matched)."""

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
    Empty for the other rows, and for a changed row past the work budget."""


def _anchors(
    a: list[str], b: list[str], i0: int, i1: int, j0: int, j1: int
) -> list[tuple[int, int]]:
    """Pairs `(i, j)` of lines that occur exactly once in `a[i0:i1]` and once in `b[j0:j1]` and
    are equal, as the longest run that is increasing on both sides (patience diff, O(n log n)).
    They are the fixed points the gaps between are compared around."""
    count_a, count_b = Counter(a[i0:i1]), Counter(b[j0:j1])
    where = {b[j]: j for j in range(j0, j1) if count_b[b[j]] == 1}
    pairs = [(i, where[a[i]]) for i in range(i0, i1) if count_a[a[i]] == 1 and a[i] in where]
    tails: list[int] = []  # the smallest end (a `j`) of an increasing run of each length
    ends: list[int] = []  # the index in `pairs` of that end
    previous = [-1] * len(pairs)
    for index, (_, j) in enumerate(pairs):
        place = bisect.bisect_left(tails, j)
        if place == len(tails):
            tails.append(j)
            ends.append(index)
        else:
            tails[place] = j
            ends[place] = index
        previous[index] = ends[place - 1] if place else -1
    chain: list[tuple[int, int]] = []
    index = ends[-1] if ends else -1
    while index != -1:
        chain.append(pairs[index])
        index = previous[index]
    chain.reverse()
    return chain


def _gap(
    a: list[str],
    b: list[str],
    i0: int,
    i1: int,
    j0: int,
    j1: int,
    out: list[Opcode],
    budget: list[int],
) -> None:
    """The opcodes of one stretch: its common start and end are cut off, the rest is matched when
    it is small enough, else it is one `block`."""
    start = 0
    limit = min(i1 - i0, j1 - j0)
    while start < limit and a[i0 + start] == b[j0 + start]:
        start += 1
    end = 0
    while end < limit - start and a[i1 - 1 - end] == b[j1 - 1 - end]:
        end += 1
    if start:
        out.append(("equal", i0, i0 + start, j0, j0 + start))
    x0, x1, y0, y1 = i0 + start, i1 - end, j0 + start, j1 - end
    if x0 < x1 or y0 < y1:
        cells = (x1 - x0) * (y1 - y0)
        if cells <= MAX_MATCH_CELLS and cells <= budget[0]:
            budget[0] -= cells
            matcher = difflib.SequenceMatcher(None, a[x0:x1], b[y0:y1], autojunk=False)
            for tag, p1, p2, q1, q2 in matcher.get_opcodes():
                out.append((tag, p1 + x0, p2 + x0, q1 + y0, q2 + y0))
        elif x0 < x1 and y0 < y1:
            out.append(("block", x0, x1, y0, y1))
        else:
            out.append(("delete" if x0 < x1 else "insert", x0, x1, y0, y1))
    if end:
        out.append(("equal", i1 - end, i1, j1 - end, j1))


def _opcodes(a: list[str], b: list[str]) -> list[Opcode]:
    """Opcodes of two lists of lines. The common start and end are cut off first (linear). In
    the middle, the lines that occur exactly once on each side and are equal are anchors (the
    longest run increasing on both sides); each gap between anchors is matched on its own when
    it is small (`MAX_MATCH_CELLS`) and is a `block` otherwise. Anchors keep one inserted line
    near the top from shifting every pair below it."""
    ops: list[Opcode] = []
    budget = [MAX_MATCH_TOTAL]
    start = 0
    limit = min(len(a), len(b))
    while start < limit and a[start] == b[start]:
        start += 1
    end = 0
    while end < limit - start and a[len(a) - 1 - end] == b[len(b) - 1 - end]:
        end += 1
    if start:
        ops.append(("equal", 0, start, 0, start))
    i1, j1 = len(a) - end, len(b) - end
    i0, j0 = start, start
    if i0 < i1 or j0 < j1:
        for i, j in [*_anchors(a, b, i0, i1, j0, j1), (i1, j1)]:
            _gap(a, b, i0, i, j0, j, ops, budget)
            if i < i1:
                ops.append(("equal", i, i + 1, j, j + 1))
            i0, j0 = i + 1, j + 1
    if end:
        ops.append(("equal", i1, len(a), j1, len(b)))
    return ops


def build_diff(official: str, proposed: str) -> list[DiffRow]:
    """The rows that compare two texts line by line. Every line of both texts is in a row that
    is `changed`, `removed` or `added`, or in an unchanged run (`same`, collapsed to `skipped`
    when it is long). A stretch that was too big to match is shown pair by pair, after an
    `unmatched` row that says so; a pair of equal lines in it is `same`. Plain strings only: the
    template escapes them."""
    a, b = official.splitlines(), proposed.splitlines()
    rows: list[DiffRow] = []
    work = 0
    for tag, i1, i2, j1, j2 in _opcodes(a, b):
        if tag == "equal":
            rows += [
                DiffRow("same", i1 + k + 1, a[i1 + k], j1 + k + 1, b[j1 + k])
                for k in range(i2 - i1)
            ]
            continue
        if tag == "block":
            rows.append(DiffRow("unmatched", skipped=max(i2 - i1, j2 - j1)))
        for k in range(max(i2 - i1, j2 - j1)):
            has_left, has_right = i1 + k < i2, j1 + k < j2
            left = a[i1 + k] if has_left else ""
            right = b[j1 + k] if has_right else ""
            left_no = i1 + k + 1 if has_left else None
            right_no = j1 + k + 1 if has_right else None
            if has_left and has_right and left == right:
                rows.append(DiffRow("same", left_no, left, right_no, right))
                continue
            kind = "changed" if has_left and has_right else "removed" if has_left else "added"
            left_parts: tuple[tuple[str, bool], ...] = ()
            right_parts: tuple[tuple[str, bool], ...] = ()
            if kind == "changed":
                cost = len(left) * len(right)
                if (
                    len(left) <= MAX_PARTS_LINE
                    and len(right) <= MAX_PARTS_LINE
                    and (work + cost <= MAX_PARTS_WORK)
                ):
                    work += cost
                    left_parts, right_parts = _parts(left, right)
            rows.append(DiffRow(kind, left_no, left, right_no, right, 0, left_parts, right_parts))
    return _collapse(rows)


def _parts(
    left: str, right: str
) -> tuple[tuple[tuple[str, bool], ...], tuple[tuple[str, bool], ...]]:
    """Each line cut into `(text, differs)` pieces by a character comparison."""
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
    """`(lines added, lines removed)` between two texts, for the summary of a queue row: a
    linear count of the lines of each text that the other does not have (as multisets, so a line
    that moved counts as neither, and a line that changed as one removed and one added). It does
    not compare the texts, so its cost does not depend on how they differ."""
    before, after = Counter(official.splitlines()), Counter(proposed.splitlines())
    return sum((after - before).values()), sum((before - after).values())


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
