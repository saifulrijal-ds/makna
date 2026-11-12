"""WP-11 (ruling 7): nothing in `makna.engine` builds SQL by string formatting or concatenation.

The freshness query is a sqlglot tree. The fixed introspection statement is one literal
constant. The scan is the strict one of `test_compiler_no_sql_strings.py` (no f-string, `%`, `+`,
`.format` or `str.join` outside the named helpers) plus its keyword scan, applied to every module
of the package. Each allowed helper returns a message for an `EngineError`, a name for output,
or the password-free text of a URL, never a piece of SQL.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import makna.engine

from .test_compiler_no_sql_strings import strict_violations, violations

PACKAGE = Path(makna.engine.__file__).parent

ALLOWED_HELPERS: dict[tuple[str, str], str] = {
    ("base.py", "redact_url"): "renders a connection URL without its password",
    ("compare.py", "name"): "dotted name of a difference, printed by the CLI",
    ("compare.py", "_bad_fqn"): "error message for a ValueError",
    ("postgres.py", "_timeout_message"): "error message for an EngineError",
    ("postgres.py", "_failure_message"): "error message for an EngineError",
    ("postgres.py", "_not_a_date"): "error message for an EngineError",
    ("postgres.py", "_bad_table"): "error message for an EngineError",
}
MODULES = sorted(PACKAGE.glob("*.py"), key=lambda p: p.name)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_the_engine_modules_use_no_string_formatting_outside_the_allowed_helpers(
    path: Path,
) -> None:
    source = path.read_text(encoding="utf-8")
    assert strict_violations(source, path.name, ALLOWED_HELPERS) == []
    assert violations(source, path.name) == []


def test_the_package_has_the_modules_the_scan_is_meant_to_cover() -> None:
    assert {"base.py", "postgres.py", "compare.py"} <= {p.name for p in MODULES}


def test_every_allowed_helper_exists_and_has_a_reason() -> None:
    for (name, function), reason in ALLOWED_HELPERS.items():
        tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
        assert function in {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert reason


def test_the_scan_catches_sql_built_from_parts_in_an_engine_module() -> None:
    bad = 'def latest(t, c):\n    return f"SELECT MAX({c}) FROM {t}"\n'
    assert strict_violations(bad, "postgres.py", ALLOWED_HELPERS)
    assert violations(bad, "postgres.py")
    assert strict_violations(bad, "other.py", ALLOWED_HELPERS)
    # An allowed name in another module is still caught.
    assert strict_violations(bad.replace("latest", "_bad_table"), "base.py", ALLOWED_HELPERS)
