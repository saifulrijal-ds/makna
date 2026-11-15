"""WP-15: the installed package never imports the eval runner (`evals`), as it never imports
`data`."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "makna"


def eval_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "evals"]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] == "evals":
                found.append(node.module or "")
    return found


def test_no_module_under_src_makna_imports_evals() -> None:
    problems = {
        str(path.relative_to(SRC)): imported
        for path in SRC.rglob("*.py")
        if (imported := eval_imports(path.read_text(encoding="utf-8")))
    }
    assert problems == {}


def test_the_check_detects_an_import() -> None:
    assert eval_imports("import evals\n") == ["evals"]
    assert eval_imports("import evals.golden.schema as s\n") == ["evals.golden.schema"]
    assert eval_imports("from evals.cli import main\n") == ["evals.cli"]
    assert eval_imports("from makna.index import tables\nimport duckdb\n") == []


def test_the_package_does_not_ship_evals() -> None:
    # `uv_build` packages the module named in pyproject.toml only; evals/ stays outside it.
    text = (ROOT / "pyproject.toml").read_text()
    assert 'module-name = "makna"' in text
