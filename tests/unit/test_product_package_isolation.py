"""WP-05: the installed package never imports the sample-data code (`data.*`)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "makna"


def data_imports(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] in ("data", "deploy")]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] in ("data", "deploy"):
                found.append(node.module or "")
    return found


def test_no_module_under_src_makna_imports_data_or_deploy() -> None:
    problems = {
        str(path.relative_to(SRC)): imported
        for path in SRC.rglob("*.py")
        if (imported := data_imports(path.read_text(encoding="utf-8")))
    }
    assert problems == {}


def test_the_check_detects_an_import() -> None:
    assert data_imports("import data.generator\n") == ["data.generator"]
    assert data_imports("from data.lending_loader import load\n") == ["data.lending_loader"]
    assert data_imports("from deploy import init\n") == ["deploy"]
    assert data_imports("from makna.index import tables\nimport duckdb\n") == []
