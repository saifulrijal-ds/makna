"""WP-11 criterion 6 (D-21): nothing in `makna.services`, `makna.mcp` or `makna.web` reaches the
engine except the freshness service.

Makna never runs a caller's query, and the only code that talks to the target engine is the
engine connector, called for freshness and introspection. This test reads the source of the
three packages with `ast` and fails on

- an import of `makna.engine` or anything below it (`import`, `from ... import`, relative
  imports, and `from makna import engine`),
- a string that names a `makna.engine` module (a dynamic import), and
- a call of a connector: `PostgresConnector(...)`, or a method call `.list_tables(...)` or
  `.latest_value(...)`.

`ALLOWED_MODULES` is empty today. The freshness service (WP-17) is the one module that will be
added to it, by name, in the pull request that builds it; any other module in the list needs a
decision record. The dependency rule (`test_dependency_rule.py`) already keeps `mcp` and `web`
from importing `engine` at all; this test also covers `services`, which that rule allows to
depend on everything, and the call sites.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .test_dependency_rule import SRC, _imported_modules, _module_name

PACKAGES = ("services", "mcp", "web")
ALLOWED_MODULES: frozenset[str] = frozenset()  # the freshness service, when it exists
CONNECTOR_METHODS = frozenset({"list_tables", "latest_value"})
CONNECTOR_CLASSES = frozenset({"PostgresConnector", "EngineConnector"})


def engine_uses(root: Path, allowed: frozenset[str] = ALLOWED_MODULES) -> list[str]:
    """Every place under `root/makna/{services,mcp,web}` that reaches the engine, outside the
    allowed modules."""
    found: list[str] = []
    for package in PACKAGES:
        for path in sorted((root / "makna" / package).rglob("*.py")):
            module = _module_name(root, path)
            if module in allowed:
                continue
            for imported in _imported_modules(path, module):
                if imported == "makna.engine" or imported.startswith("makna.engine."):
                    found.append(f"{module}: imports {imported}")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                line = getattr(node, "lineno", "?")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value == "makna.engine" or node.value.startswith("makna.engine."):
                        found.append(f"{module}:{line}: names the module {node.value!r}")
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr in CONNECTOR_METHODS:
                        found.append(f"{module}:{line}: calls .{func.attr}()")
                    if isinstance(func, ast.Attribute) and func.attr in CONNECTOR_CLASSES:
                        found.append(f"{module}:{line}: calls {func.attr}()")
                    if isinstance(func, ast.Name) and func.id in CONNECTOR_CLASSES:
                        found.append(f"{module}:{line}: calls {func.id}()")
    return found


def test_no_interface_module_reaches_the_engine() -> None:
    assert engine_uses(SRC) == []


def test_the_allowlist_is_empty_until_the_freshness_service_exists() -> None:
    assert ALLOWED_MODULES == frozenset()


def test_the_packages_the_test_walks_exist_and_hold_modules() -> None:
    for package in PACKAGES:
        assert list((SRC / "makna" / package).rglob("*.py")), package


def write(root: Path, relative: str, source: str) -> None:
    path = root / "makna" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    for parent in (root / "makna", *path.parents):
        if parent.is_relative_to(root):
            (parent / "__init__.py").touch()


@pytest.mark.parametrize(
    ("relative", "source"),
    [
        ("services/a.py", "import makna.engine.postgres\n"),
        ("services/a.py", "from makna.engine.postgres import PostgresConnector\n"),
        ("services/a.py", "from makna import engine\n"),
        ("services/a.py", "from ..engine import base\n"),
        ("mcp/a.py", "from makna.engine import base\n"),
        ("web/sub/a.py", "import makna.engine\n"),
        ("services/a.py", "import importlib\nimportlib.import_module('makna.engine.postgres')\n"),
        ("services/a.py", "def f(c):\n    return c.latest_value('t', e)\n"),
        ("web/a.py", "def f(c):\n    return c.list_tables('core')\n"),
        ("mcp/a.py", "def f():\n    return PostgresConnector('u')\n"),
        ("services/a.py", "def f(m):\n    return m.PostgresConnector('u')\n"),
    ],
)
def test_a_deliberately_bad_module_is_caught(tmp_path: Path, relative: str, source: str) -> None:
    write(tmp_path, relative, source)
    assert engine_uses(tmp_path), source


def test_the_allowlist_exempts_only_the_named_module(tmp_path: Path) -> None:
    write(tmp_path, "services/freshness.py", "from makna.engine.base import EngineConnector\n")
    write(tmp_path, "services/other.py", "from makna.engine.base import EngineConnector\n")
    found = engine_uses(tmp_path, frozenset({"makna.services.freshness"}))
    assert found == ["makna.services.other: imports makna.engine.base"]


def test_other_imports_and_calls_pass(tmp_path: Path) -> None:
    write(
        tmp_path,
        "services/a.py",
        "from makna.compiler import compile_request\nfrom makna.model import x\n"
        "def f(session):\n    return session.execute('select 1')\n",
    )
    assert engine_uses(tmp_path) == []
