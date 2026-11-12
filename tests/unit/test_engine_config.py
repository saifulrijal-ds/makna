"""WP-11: the `[engine]` setting and the loader the CLI uses."""

from __future__ import annotations

from pathlib import Path

import pytest

from makna.config import ConfigError, load_engine_runtime, load_settings

ROOT = Path(__file__).resolve().parents[2]
ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@h/m", "MAKNA_SECRET_KEY": "k"}


def test_the_committed_config_sets_the_timeout() -> None:
    runtime = load_engine_runtime({}, ROOT / "config" / "makna.toml")
    assert runtime.engine.statement_timeout_seconds == 30
    assert runtime.engine_url is None
    assert load_settings(ENV, ROOT / "config" / "makna.toml").engine.statement_timeout_seconds == 30


def test_the_url_comes_from_the_environment_and_is_not_printed(tmp_path: Path) -> None:
    env = {"MAKNA_ENGINE_URL": "postgresql://ro:hunter2@h/lending"}
    runtime = load_engine_runtime(env, tmp_path / "none.toml")
    assert runtime.engine_url is not None
    assert runtime.engine_url.get_secret_value().endswith("@h/lending")
    assert "hunter2" not in repr(runtime)
    assert runtime.engine.statement_timeout_seconds == 30


def test_the_timeout_can_be_set_and_must_be_positive(tmp_path: Path) -> None:
    config = tmp_path / "m.toml"
    config.write_text("[engine]\nstatement_timeout_seconds = 5\n")
    assert load_engine_runtime({}, config).engine.statement_timeout_seconds == 5
    config.write_text("[engine]\nstatement_timeout_seconds = 0\n")
    with pytest.raises(ConfigError, match="statement_timeout_seconds"):
        load_engine_runtime({}, config)


def test_the_loader_needs_no_application_secrets(tmp_path: Path) -> None:
    load_engine_runtime({}, tmp_path / "none.toml")
