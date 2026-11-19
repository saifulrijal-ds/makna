"""WP-01 criterion 3: the dependency rule of ARCHITECTURE section 2, checked on imports."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# Database and network libraries the compiler must not import (it is pure functions over models).
DB_NETWORK_LIBRARIES = frozenset(
    {
        "sqlalchemy", "alembic", "psycopg", "psycopg2", "psycopg_pool", "asyncpg", "pgvector",
        "duckdb", "sqlite3", "pymysql", "mysql", "trino", "pyodbc",
        "httpx", "requests", "urllib3", "aiohttp", "httplib2", "websockets",
        "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "xmlrpc",
    }
)  # fmt: skip

# Allowed makna packages per package. None means "no restriction from this rule".
ALLOWED_MAKNA: dict[str, frozenset[str] | None] = {
    "mcp": frozenset({"services", "model", "config"}),
    "web": frozenset({"services", "model", "config"}),
    "cli": frozenset(
        {
            "services",
            "model",
            "config",
            "definitions",
            "catalog",
            "index",
            "search",
            "engine",
            "app",
        }
    ),
    # The composition root mounts the interfaces; it is the only module that may import both.
    "app": frozenset({"services", "web", "mcp", "model", "config"}),
    "compiler": frozenset({"model"}),
    # The definition store reads and writes files and calls git; it knows only the models.
    "definitions": frozenset({"model"}),
    "engine": frozenset({"model"}),
    "catalog": frozenset({"model", "index"}),
    "index": frozenset({"model"}),
    "search": frozenset({"model"}),
    # The resolver reads a search result and the compiler's snapshot and available dimensions.
    "resolve": frozenset({"model", "search", "compiler"}),
    "model": frozenset(),
    # The use cases depend on every library package, never on an interface (WP-13).
    "services": frozenset(
        {
            "model",
            "config",
            "definitions",
            "catalog",
            "index",
            "search",
            "engine",
            "compiler",
            "resolve",
        }
    ),
}
# Single modules a package may import although it may not import the rest of their package:
# `makna mcp` is only an HTTP bridge (ARCHITECTURE section 12), so the command line may load it.
ALLOWED_MODULES: dict[str, tuple[str, ...]] = {"cli": ("makna.mcp.bridge",)}
# Modules that import nothing from Makna at all. The stdio bridge runs on an agent's machine and
# must not hold service code or database settings (WP-13); a second test loads it in a fresh
# interpreter and looks at what it pulled in.
NO_MAKNA_IMPORTS = frozenset({"makna/mcp/bridge.py"})
FORBIDDEN_THIRD_PARTY: dict[str, frozenset[str]] = {"compiler": DB_NETWORK_LIBRARIES}


@dataclass(frozen=True)
class Violation:
    file: str
    package: str
    imported: str

    def __str__(self) -> str:
        return f"{self.file}: makna.{self.package} must not import {self.imported}"


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path, module: str) -> list[str]:
    """Absolute dotted names imported by a file; relative imports are resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            for alias in node.names:
                # `from makna import services` imports makna.services; keep the name.
                found.append(f"{prefix}.{alias.name}" if prefix in ("makna", "") else prefix)
    return found


def check_dependency_rule(root: Path) -> list[Violation]:
    """Check every module under `root/makna` against the dependency rule."""
    violations: list[Violation] = []
    for path in sorted((root / "makna").rglob("*.py")):
        module = _module_name(root, path)
        parts = module.split(".")
        if len(parts) < 2:
            continue
        package = parts[1]
        allowed = ALLOWED_MAKNA.get(package)
        forbidden = FORBIDDEN_THIRD_PARTY.get(package, frozenset())
        for imported in _imported_modules(path, module):
            top = imported.split(".")[0]
            if top == "makna":
                relative = str(path.relative_to(root))
                if relative in NO_MAKNA_IMPORTS:
                    violations.append(Violation(relative, package, imported))
                    continue
                target = imported.split(".")[1] if "." in imported else None
                if target is None or target == package:
                    continue
                if allowed is not None and target not in allowed:
                    if any(
                        imported == m or imported.startswith(m + ".")
                        for m in ALLOWED_MODULES.get(package, ())
                    ):
                        continue
                    violations.append(Violation(relative, package, imported))
            elif top in forbidden:
                violations.append(Violation(str(path.relative_to(root)), package, imported))
    return violations


def _write(root: Path, relative: str, text: str) -> None:
    path = root / "makna" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_real_tree_follows_the_dependency_rule() -> None:
    assert check_dependency_rule(SRC) == []


def test_every_package_of_the_architecture_exists() -> None:
    expected = {
        "model", "definitions", "catalog", "engine", "index", "search", "compiler",
        "resolve", "services", "mcp", "web", "cli",
    }  # fmt: skip
    found = {p.name for p in (SRC / "makna").iterdir() if (p / "__init__.py").exists()}
    assert expected <= found


@pytest.mark.parametrize(
    ("relative", "source", "imported"),
    [
        ("mcp/tools.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("mcp/tools.py", "import makna.search.fusion\n", "makna.search.fusion"),
        ("web/routes.py", "from makna.index import tables\n", "makna.index"),
        ("web/sub/deep.py", "from makna import engine\n", "makna.engine"),
        ("web/routes.py", "from ..catalog import sync\n", "makna.catalog"),
        ("cli/commands.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("cli/commands.py", "from makna.resolve import resolver\n", "makna.resolve"),
        ("cli/commands.py", "from makna.web import routes\n", "makna.web"),
        ("cli/commands.py", "import makna.mcp.server\n", "makna.mcp.server"),
        ("cli/commands.py", "from makna.mcp.http import build_mcp_endpoint\n", "makna.mcp.http"),
        ("mcp/tools.py", "from makna.index import tables\n", "makna.index"),
        ("mcp/tools.py", "from makna.catalog import base\n", "makna.catalog"),
        ("mcp/tools.py", "from makna.engine import base\n", "makna.engine"),
        ("mcp/tools.py", "from makna.app import create_app\n", "makna.app"),
        ("web/routes.py", "from makna.mcp import http\n", "makna.mcp"),
        ("web/routes.py", "from makna.app import create_app\n", "makna.app"),
        ("web/gate.py", "from makna.index.tables import app_user\n", "makna.index.tables"),
        ("web/pages.py", "from makna.definitions import loader\n", "makna.definitions"),
        ("web/routes.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("web/routes.py", "from makna.search import hybrid\n", "makna.search"),
        ("web/security.py", "from makna.cli import main\n", "makna.cli"),
        ("mcp/http.py", "from makna.web import gate\n", "makna.web"),
        ("mcp/bridge.py", "from makna.model import request\n", "makna.model"),
        ("mcp/bridge.py", "from makna.services import Caller\n", "makna.services"),
        ("mcp/bridge.py", "from makna.mcp import texts\n", "makna.mcp"),
        ("definitions/x.py", "from makna.services import lifecycle\n", "makna.services"),
        ("definitions/x.py", "from makna.index import indexer\n", "makna.index"),
        ("definitions/x.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("services/x.py", "from makna.mcp import server\n", "makna.mcp"),
        ("services/x.py", "from makna.web import routes\n", "makna.web"),
        ("services/x.py", "from makna.app import create_app\n", "makna.app"),
        ("services/x.py", "from makna.cli import main\n", "makna.cli"),
        ("app.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("app.py", "from makna.index import tables\n", "makna.index"),
        ("compiler/sql.py", "import sqlalchemy\n", "sqlalchemy"),
        ("compiler/sql.py", "import psycopg\n", "psycopg"),
        ("compiler/net.py", "import httpx\n", "httpx"),
        ("compiler/net.py", "from urllib.request import urlopen\n", "urllib.request"),
        ("compiler/net.py", "import socket\n", "socket"),
        ("compiler/x.py", "from makna.search import hybrid\n", "makna.search"),
        ("engine/x.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("engine/x.py", "from makna.config import Settings\n", "makna.config"),
        ("engine/x.py", "from makna.catalog import base\n", "makna.catalog"),
        ("model/x.py", "from makna.config import Settings\n", "makna.config"),
        ("catalog/x.py", "from makna.definitions import loader\n", "makna.definitions"),
        ("catalog/x.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("catalog/x.py", "from makna.services import search\n", "makna.services"),
        ("index/x.py", "from makna.definitions import loader\n", "makna.definitions"),
        ("index/x.py", "from makna.search import embedding\n", "makna.search"),
        ("index/x.py", "from makna.compiler import plan\n", "makna.compiler"),
        ("search/x.py", "from makna.index import tables\n", "makna.index"),
        ("search/x.py", "from makna.definitions import loader\n", "makna.definitions"),
        ("search/x.py", "from makna.services import search\n", "makna.services"),
    ],
)
def test_a_deliberately_bad_module_is_caught(
    tmp_path: Path, relative: str, source: str, imported: str
) -> None:
    _write(tmp_path, "__init__.py", "")
    _write(tmp_path, relative, source)
    violations = check_dependency_rule(tmp_path)
    assert [v.imported for v in violations] == [imported], violations


def test_allowed_imports_pass(tmp_path: Path) -> None:
    _write(tmp_path, "__init__.py", "")
    _write(tmp_path, "mcp/a.py", "from makna.services import x\nfrom makna.model import y\n")
    _write(tmp_path, "web/a.py", "from makna import config\nfrom makna.config import Settings\n")
    _write(tmp_path, "web/b.py", "from . import a\nfrom .a import thing\n")
    _write(
        tmp_path,
        "web/c.py",
        "from makna.services import accounts, permissions\n"
        "from makna.services.accounts import User\n"
        "from makna.model.request import QueryRequest\n"
        "from makna.config import Settings\n",
    )
    _write(tmp_path, "cli/a.py", "from makna.definitions import v\nfrom makna.engine import e\n")
    _write(tmp_path, "cli/b.py", "from makna.app import create_app\n")
    _write(tmp_path, "cli/c.py", "from makna.mcp.bridge import main\n")
    _write(tmp_path, "mcp/bridge.py", "import os\nimport anyio\n")
    _write(
        tmp_path, "services/a.py", "from makna.compiler import plan\nfrom makna.index import t\n"
    )
    _write(tmp_path, "app.py", "from makna.web import w\nfrom makna.mcp import m\n")
    _write(tmp_path, "compiler/a.py", "import sqlglot\nfrom makna.model import m\n")
    _write(tmp_path, "catalog/a.py", "from makna.model import m\nfrom makna.index import tables\n")
    _write(tmp_path, "index/a.py", "from makna.model import m\nfrom . import b\n")
    _write(tmp_path, "search/a.py", "from makna.model.search import S\nfrom . import b\n")
    _write(tmp_path, "engine/a.py", "from makna.model import m\nfrom . import b\n")
    assert check_dependency_rule(tmp_path) == []


def test_only_the_git_wrapper_starts_a_program() -> None:
    """WP-18: `makna.definitions.repo` is the one place that runs a command, and it never uses a
    shell. Nothing in the web app or the MCP server can start a process or call git."""
    pattern = re.compile(
        r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|os\.spawn\w*|pty|shell\s*=\s*True)\b"
    )
    found = {
        str(path.relative_to(SRC))
        for path in (SRC / "makna").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert found == {"makna/definitions/repo.py"}
