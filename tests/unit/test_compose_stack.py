"""WP-05 criteria 5 and 6 (static part): what the committed stack files may and may not say."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)(:?[-?])([^}]*)\}")

# The only variables allowed to carry a development default: local PostgreSQL passwords
# (and the host port).
DEFAULT_ALLOWED = re.compile(
    r"MAKNA_(POSTGRES|DB|LENDING_LOADER|LENDING_READER)_PASSWORD|MAKNA_POSTGRES_PORT"
)


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def test_only_local_postgres_passwords_have_development_defaults() -> None:
    text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    defaults = {m.group(1) for m in VARIABLE.finditer(text) if m.group(2) == ":-" and m.group(3)}
    assert defaults, "expected the PostgreSQL password defaults"
    assert all(DEFAULT_ALLOWED.fullmatch(name) for name in defaults), defaults


def test_secret_key_is_required_and_has_no_default() -> None:
    text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${MAKNA_SECRET_KEY:?" in text
    assert not re.search(r"MAKNA_SECRET_KEY:-", text)


def test_published_ports_are_bound_to_loopback() -> None:
    for name, service in COMPOSE["services"].items():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    assert COMPOSE["services"]["app"]["ports"] == ["127.0.0.1:8000:8000"]
    assert any(":5432" in str(p) for p in COMPOSE["services"]["postgres"]["ports"])


def test_the_three_services_and_no_trino() -> None:
    assert set(COMPOSE["services"]) == {"postgres", "init", "app"}
    assert "trino" not in "".join(_strings(COMPOSE)).lower()


def test_postgres_image_is_pinned_and_recorded_in_the_readme() -> None:
    image = COMPOSE["services"]["postgres"]["image"]
    assert image == "pgvector/pgvector:0.8.0-pg17"
    assert image in (DEPLOY / "README.md").read_text(encoding="utf-8")


def test_the_dockerfile_pins_uv_to_the_ci_version() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    version = re.search(r'version: "([\d.]+)"', ci)
    assert version is not None
    assert f"uv=={version.group(1)}" in dockerfile


def test_init_and_app_wait_in_order() -> None:
    services = COMPOSE["services"]
    assert services["init"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["app"]["depends_on"]["init"]["condition"] == "service_completed_successfully"


def test_role_script_has_no_literal_passwords() -> None:
    script = (DEPLOY / "postgres-init" / "01-roles.sh").read_text(encoding="utf-8")
    assert not re.search(r"PASSWORD\s+'", script)
    for role in ("makna", "lending_loader", "lending_reader"):
        assert f"CREATE ROLE {role} LOGIN PASSWORD :'" in script


def test_env_example_lists_the_stack_variables() -> None:
    names = {
        line.partition("=")[0]
        for line in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    }
    used = set(VARIABLE.findall((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")))
    compose_inputs = {name for name, _, _ in used}
    assert compose_inputs <= names, compose_inputs - names
    assert {"MAKNA_LENDING_LOADER_URL", "MAKNA_POSTGRES_PORT"} <= names
