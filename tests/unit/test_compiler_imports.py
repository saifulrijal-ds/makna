"""WP-08 criterion 5: the planning modules import no I/O library.

`test_dependency_rule.py` forbids database and network libraries in `makna.compiler`. This test is
stricter for the modules planning is made of: they may import only an allowed list, so a new
import (a file, process, clock, or random library) has to be a visible decision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "makna"
PLANNING_MODULES = [
    SRC / "compiler" / "snapshot.py",
    SRC / "compiler" / "graph.py",
    SRC / "compiler" / "plan.py",
    SRC / "model" / "request.py",
]
GENERATION_MODULES = [
    SRC / "compiler" / "sqlgen.py",
    SRC / "compiler" / "explain.py",
    SRC / "compiler" / "compile.py",
]
ALLOWED = frozenset(
    {
        "__future__", "collections", "dataclasses", "datetime", "difflib", "enum", "math", "re",
        "types", "typing", "pydantic", "makna",
    }
)  # fmt: skip


# SQL generation may also use sqlglot, which parses and prints text and does no I/O (WP-09).
GENERATION_ALLOWED = ALLOWED | {"functools", "sqlglot"}


def top_level_imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("path", PLANNING_MODULES, ids=lambda p: p.name)
def test_planning_modules_import_only_pure_libraries(path: Path) -> None:
    assert top_level_imports(path.read_text(encoding="utf-8")) <= ALLOWED


@pytest.mark.parametrize("path", PLANNING_MODULES, ids=lambda p: p.name)
def test_planning_modules_import_only_model_from_makna(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    makna = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("makna")
    }
    assert all(m.startswith(("makna.model", "makna.compiler")) for m in makna), makna


def test_the_check_notices_an_io_library() -> None:
    assert top_level_imports("import os\nfrom pathlib import Path\n") == {"os", "pathlib"}
    assert not {"os", "pathlib", "socket", "sqlalchemy", "random", "time"} & ALLOWED


@pytest.mark.parametrize("path", GENERATION_MODULES, ids=lambda p: p.name)
def test_generation_modules_import_only_pure_libraries_and_sqlglot(path: Path) -> None:
    assert top_level_imports(path.read_text(encoding="utf-8")) <= GENERATION_ALLOWED


@pytest.mark.parametrize("path", GENERATION_MODULES, ids=lambda p: p.name)
def test_generation_modules_import_only_model_from_makna(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    makna = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("makna")
    }
    assert all(m.startswith(("makna.model", "makna.compiler")) for m in makna), makna
