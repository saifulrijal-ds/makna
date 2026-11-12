"""WP-09 criterion 9: nothing in `makna.compiler` builds SQL by string formatting or concatenation.

The scan reads the package source with `ast`. It flags an f-string, a `%` or `.format` on a
string literal, and `+` with a string literal, when the literal text holds a SQL keyword:

- an upper-case keyword (SELECT, FROM, WHERE, JOIN, AND, ...) as a whole word, or
- in any case, a phrase that only SQL uses (`group by`, `order by`, `union all`, `insert into`,
  `delete from`, `drop table`, `select ... from`).

Plain English in an error message may use words like "from" or "and", so a lone lower-case word
is not enough to flag. Names assembled from parts (`f"t{n}"`, a CTE called `m_<metric>`) hold no
keyword and pass: they become quoted identifier nodes, not SQL text.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import makna.compiler

PACKAGE = Path(makna.compiler.__file__).parent

UPPER_KEYWORDS = re.compile(
    r"\b(SELECT|FROM|WHERE|JOIN|ON|GROUP BY|ORDER BY|HAVING|UNION|LIMIT|OFFSET|WITH|AS|AND|OR|NOT"
    r"|IN|IS|NULL|CASE|WHEN|THEN|ELSE|END|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|VALUES|SET"
    r"|BETWEEN|LIKE|DISTINCT)\b"
)
ANY_CASE_PHRASES = re.compile(
    r"\b(group\s+by|order\s+by|union\s+all|insert\s+into|delete\s+from|drop\s+table"
    r"|select\b.*\bfrom)\b",
    re.IGNORECASE | re.DOTALL,
)


def has_sql(text: str) -> bool:
    return bool(UPPER_KEYWORDS.search(text) or ANY_CASE_PHRASES.search(text))


def literal_text(node: ast.AST) -> str | None:
    """The fixed text of a string literal or of the literal parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def violations(source: str, name: str = "<source>") -> list[str]:
    """Where `source` builds text holding a SQL keyword by formatting or concatenation."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        text: str | None = None
        how = ""
        if isinstance(node, ast.JoinedStr):
            text, how = literal_text(node), "an f-string"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            text, how = literal_text(node.left), "% formatting"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            text = " ".join(t for t in (literal_text(node.left), literal_text(node.right)) if t)
            how = "string concatenation"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
        ):
            text, how = literal_text(node.func.value), ".format"
        if text and has_sql(text):
            found.append(f"{name}:{getattr(node, 'lineno', '?')}: {how} on {text!r}")
    return found


# --------------------------------------------------------------------------------------
# The strict rule for the modules that build SQL: no string formatting at all, except in the
# few helpers below. Each returns an identifier that is passed to sqlglot (`exp.to_identifier`,
# `exp.table_`) or a plain message for a QueryError, never a piece of SQL.

STRICT_MODULES = ("sqlgen.py", "compile.py", "governance.py")
ALLOWED_HELPERS: dict[tuple[str, str], str] = {
    ("sqlgen.py", "_failed"): "error message for a QueryError",
    ("sqlgen.py", "_refused"): "error message for a QueryError",
    ("sqlgen.py", "_alias"): "table alias text, becomes a quoted identifier node",
    ("sqlgen.py", "_cte"): "CTE name text, becomes a quoted identifier node",
    ("governance.py", "_violation"): "error message for the internal governance error",
    ("compile.py", "_metric_id"): "the `metric:<name>` reference of the response, not SQL",
}


def _is_text(node: ast.AST) -> bool:
    return literal_text(node) is not None


def strict_violations(source: str, name: str) -> list[str]:
    """Every f-string, `%`/`+` with a string operand, `.format`, and string `.join` in `source`
    that is not inside one of the allowed helpers."""
    found: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        how = None
        if isinstance(node, ast.JoinedStr):
            how = "an f-string"
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            if _is_text(node.left) or _is_text(node.right):
                how = "% or + with a string operand"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format" and _is_text(node.func.value):
                how = ".format"
            elif node.func.attr == "join" and _is_text(node.func.value):
                how = "str.join"
        if how and (name, function or "") not in ALLOWED_HELPERS:
            found.append(f"{name}:{getattr(node, 'lineno', '?')} in {function}: {how}")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(source), None)
    return found


@pytest.mark.parametrize("name", STRICT_MODULES)
def test_the_modules_that_build_sql_use_no_string_formatting_outside_the_allowed_helpers(
    name: str,
) -> None:
    assert strict_violations((PACKAGE / name).read_text(encoding="utf-8"), name) == []


def test_every_allowed_helper_exists_and_has_a_reason() -> None:
    for (name, function), reason in ALLOWED_HELPERS.items():
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        assert function in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert reason


@pytest.mark.parametrize(
    "bad",
    [
        'def q(a):\n    return f"SUM({a})"\n',
        'def q(g, c):\n    return f"date_trunc(\'{g}\', {c})"\n',
        'def q(c, v):\n    return f"{c} = \'{v}\'"\n',
        'def q(a, b):\n    return f"({a}) and ({b})"\n',
        'def q(cols):\n    return ", ".join(cols)\n',
        'def q(a, b):\n    return a + "." + b\n',
        'def q(a):\n    return "x(%s)" % a\n',
        'def q(a):\n    return "x({})".format(a)\n',
        'def _alias(a):\n    return f"SUM({a})"\n',
    ],
    ids=["call", "call with quotes", "equals", "and", "join", "concat", "percent", "format",
         "allowed name in the wrong module"],
)  # fmt: skip
def test_the_strict_scan_catches_what_the_keyword_scan_misses(bad: str) -> None:
    module = "other.py" if "_alias" in bad else "sqlgen.py"
    assert strict_violations(bad, module), f"the strict scan missed: {bad}"


def test_the_strict_scan_allows_the_named_helpers_only() -> None:
    assert strict_violations('def _alias(n):\n    return f"t{n}"\n', "sqlgen.py") == []
    assert strict_violations('def other(n):\n    return f"t{n}"\n', "sqlgen.py")


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_no_function_builds_sql_with_string_formatting(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8"), path.name) == []


def test_the_package_has_the_modules_the_scan_is_meant_to_cover() -> None:
    names = {p.name for p in PACKAGE.glob("*.py")}
    assert {"sqlgen.py", "explain.py", "plan.py", "graph.py"} <= names


@pytest.mark.parametrize(
    "bad",
    [
        'def q(t):\n    return f"SELECT a FROM {t}"\n',
        'def q(t):\n    return f"select a from {t}"\n',
        'def q(x):\n    return "SELECT * FROM t WHERE a = %s" % x\n',
        'def q(x):\n    return "SELECT * FROM t WHERE a = {}".format(x)\n',
        'def q(x):\n    return "SELECT a FROM t WHERE a = " + x\n',
        'def q(x):\n    return x + " ORDER BY a"\n',
        'def q(a, b):\n    return f"{a} AND {b}"\n',
        'def q(a):\n    return "select a " + a + " group by 1"\n',
    ],
    ids=[
        "f-string",
        "lower-case f-string",
        "percent",
        "format",
        "concat",
        "concat-left",
        "and",
        "lower",
    ],
)
def test_the_scan_catches_a_violation(bad: str) -> None:
    assert violations(bad), f"the scan missed: {bad}"


@pytest.mark.parametrize(
    "good",
    [
        'def m(name):\n    return f"Metric {name} is deprecated."\n',
        'def m(n):\n    return f"t{n}"\n',
        'def m(name):\n    return f"m_{name}"\n',
        'def m(a, b):\n    return f"Rows come from {a} and {b}."\n',
        "def m(a):\n    return exp.select(a).from_(a)\n",
        'SQL = "SELECT 1"\n',
    ],
    ids=["message", "alias", "cte name", "english", "tree builder", "plain constant"],
)
def test_the_scan_leaves_names_and_messages_alone(good: str) -> None:
    assert violations(good) == []
