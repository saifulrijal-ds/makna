"""WP-07: what the committed stack files say about the embedding model and the offline check."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from makna.config import load_search_runtime

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose files, whose `!reset` tag the plain loader does not know."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: "<reset>")


def override() -> dict[str, Any]:
    text = (ROOT / "deploy" / "docker-compose.offline.yml").read_text(encoding="utf-8")
    loaded = yaml.load(text, Loader=_ComposeLoader)  # noqa: S506 - a loader with the safe base
    assert isinstance(loaded, dict)
    return loaded


def test_the_image_downloads_the_model_the_settings_name_into_the_directory_they_name() -> None:
    fastembed = load_search_runtime({}, ROOT / "config" / "makna.toml").embedding.fastembed
    model = re.search(r"^ARG MAKNA_EMBEDDING_MODEL=(\S+)$", DOCKERFILE, re.MULTILINE)
    cache = re.search(r"^ENV MAKNA_EMBEDDING_CACHE_DIR=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert model is not None and cache is not None
    assert model.group(1) == fastembed.model
    assert cache.group(1) == fastembed.cache_dir


def test_the_model_layer_comes_before_the_source_and_the_settings() -> None:
    """Editing `src` or `config` must not fetch the 0.22 GB model again."""
    deps = DOCKERFILE.index("uv sync --locked --no-dev --no-install-project")
    download = DOCKERFILE.index("TextEmbedding(model_name=")
    offline = DOCKERFILE.index("ENV HF_HUB_OFFLINE=1")
    source = DOCKERFILE.index("COPY src ./src")
    config = DOCKERFILE.index("COPY config ./config")
    user = DOCKERFILE.index("USER makna")
    assert deps < download < offline < source < config < user
    assert "chown -R makna /opt/makna/models" in DOCKERFILE
    assert DOCKERFILE.index("RUN uv sync --locked --no-dev\n") > source
    assert "COPY pyproject.toml uv.lock" in DOCKERFILE[:deps]


def test_the_offline_override_makes_the_network_internal_and_publishes_nothing() -> None:
    compose = override()
    assert compose["networks"]["default"]["internal"] is True
    assert compose["name"] == "makna-offline"
    assert compose["services"]["postgres"]["ports"] == "<reset>"
    assert compose["services"]["app"]["ports"] == "<reset>"
    assert set(compose["services"]) <= {"postgres", "init", "app"}
    assert "--scale" in compose["services"]["init"]["command"]


def test_ci_runs_the_search_checks_online_and_offline() -> None:
    assert "exec -T app python -m deploy.check_search\n" in CI
    assert "exec -T app python -m deploy.check_search --require-offline" in CI
    assert "docker-compose.offline.yml" in CI
    assert "exec -T app makna index build" in CI
    assert 'exec -T app makna search "pembiayaan bermasalah"' in CI
    assert CI.index("deploy.check_stack") < CI.index("docker-compose.offline.yml")


def test_the_env_example_names_the_embedding_variables_without_values() -> None:
    text = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^MAKNA_EMBEDDING_API_KEY=$", text, re.MULTILINE)
    assert re.search(r"^MAKNA_EMBEDDING_CACHE_DIR=$", text, re.MULTILINE)
