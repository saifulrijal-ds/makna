"""WP-01 criterion 5 and the settings loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from makna.config import ConfigError, load_search_runtime, load_settings

GOOD_ENV = {
    "MAKNA_DATABASE_URL": "postgresql://u:p@localhost/makna",
    "MAKNA_SECRET_KEY": "not-a-real-secret",
}
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["MAKNA_DATABASE_URL", "MAKNA_SECRET_KEY"])
def test_missing_required_variable_is_named(name: str, tmp_path: Path) -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != name}
    with pytest.raises(ConfigError, match=name):
        load_settings(env, tmp_path / "none.toml")


def test_empty_required_variable_counts_as_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="MAKNA_SECRET_KEY"):
        load_settings({**GOOD_ENV, "MAKNA_SECRET_KEY": ""}, tmp_path / "none.toml")


def test_all_missing_variables_are_named(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_settings({}, tmp_path / "none.toml")
    assert "MAKNA_DATABASE_URL" in str(info.value)
    assert "MAKNA_SECRET_KEY" in str(info.value)


def test_defaults_without_a_config_file(tmp_path: Path) -> None:
    settings = load_settings(GOOD_ENV, tmp_path / "none.toml")
    assert settings.database_url.get_secret_value() == GOOD_ENV["MAKNA_DATABASE_URL"]
    assert settings.engine_url is None
    assert settings.dialect == "postgres"
    assert settings.governance.min_group_size == 10


def test_secrets_are_not_printed(tmp_path: Path) -> None:
    settings = load_settings(GOOD_ENV, tmp_path / "none.toml")
    assert "not-a-real-secret" not in repr(settings)


def test_toml_and_env_are_combined(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("[governance]\nmin_group_size = 25\n[search]\nw_vector = 0.5\n")
    env = {**GOOD_ENV, "MAKNA_ENGINE_URL": "postgresql://ro@localhost/lending"}
    settings = load_settings(env, config)
    assert settings.governance.min_group_size == 25
    assert settings.search.w_vector == 0.5
    assert settings.engine_url is not None


def test_invalid_toml_value_names_the_field(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("min_group_size = 'x'\n")
    with pytest.raises(ConfigError, match="min_group_size"):
        load_settings(GOOD_ENV, config)


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("this is = = not toml")
    with pytest.raises(ConfigError, match="makna.toml"):
        load_settings(GOOD_ENV, config)


def test_the_committed_config_file_loads() -> None:
    settings = load_settings(GOOD_ENV, ROOT / "config" / "makna.toml")
    assert settings.sensitivity.tag_map["PII.Sensitive"] == "personal"


def test_defaults_follow_the_architecture(tmp_path: Path) -> None:
    settings = load_settings(GOOD_ENV, tmp_path / "none.toml")
    assert settings.embedding_provider == "fastembed"
    assert settings.allow_self_approval is False


def test_self_approval_can_be_turned_on(tmp_path: Path) -> None:
    env = {**GOOD_ENV, "MAKNA_ALLOW_SELF_APPROVAL": "true"}
    assert load_settings(env, tmp_path / "none.toml").allow_self_approval is True


def test_unknown_embedding_provider_names_the_variable(tmp_path: Path) -> None:
    env = {**GOOD_ENV, "MAKNA_EMBEDDING_PROVIDER": "local"}
    with pytest.raises(ConfigError, match="MAKNA_EMBEDDING_PROVIDER"):
        load_settings(env, tmp_path / "none.toml")


def test_search_and_embedding_settings_have_a_section_each(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text(
        'embedding_provider = "hash"\n'
        "[search]\nw_keyword = 0.5\n[search.kind_weights]\nmetric = 2.0\n"
        "[search.confidence]\nhigh = 0.7\n"
        '[embedding.ollama]\nurl = "http://ollama:11434"\nmodel = "m"\n'
        '[embedding.fastembed]\ncache_dir = "/models"\n'
    )
    settings = load_settings(
        {"MAKNA_DATABASE_URL": "postgresql://x", "MAKNA_SECRET_KEY": "k"}, config
    )
    assert settings.embedding_provider == "hash"
    assert settings.search.w_keyword == 0.5
    assert settings.search.kind_weights == {"metric": 2.0}
    assert settings.search.confidence.high == 0.7
    assert settings.embedding.ollama.url == "http://ollama:11434"
    assert settings.embedding.fastembed.cache_dir == "/models"


def test_the_environment_overrides_provider_cache_dir_and_key(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text('embedding_provider = "fastembed"\n[embedding.fastembed]\nmodel = "m"\n')
    env = {
        "MAKNA_EMBEDDING_PROVIDER": "openai_compatible",
        "MAKNA_EMBEDDING_CACHE_DIR": "/mnt/models",
        "MAKNA_EMBEDDING_API_KEY": "sk-test",
    }
    runtime = load_search_runtime(env, config)  # needs no database URL or secret key
    assert runtime.embedding_provider == "openai_compatible"
    assert runtime.embedding.fastembed.cache_dir == "/mnt/models"
    assert runtime.embedding.fastembed.model == "m"
    assert runtime.embedding_api_key is not None
    assert runtime.embedding_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(runtime)


def test_the_committed_config_names_the_defaults() -> None:
    runtime = load_search_runtime({}, Path(__file__).resolve().parents[2] / "config" / "makna.toml")
    assert runtime.embedding_provider == "fastembed"
    assert runtime.embedding.fastembed.model.endswith("paraphrase-multilingual-MiniLM-L12-v2")
    assert runtime.embedding.fastembed.allow_download is False
    assert runtime.search.w_keyword == runtime.search.w_vector == 1.0
    assert runtime.search.status_weights["draft"] == 0.5
    assert runtime.search.kind_weights["column"] == 0.6


def test_a_bad_search_setting_is_reported(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("[search]\nw_keyword = -1\n")
    with pytest.raises(ConfigError, match="search.w_keyword"):
        load_search_runtime({}, config)


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("true", True), ("false", False)])
def test_allow_download_can_be_set_from_the_environment(
    value: str, expected: bool, tmp_path: Path
) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("[embedding.fastembed]\nallow_download = false\n")
    runtime = load_search_runtime({"MAKNA_EMBEDDING_ALLOW_DOWNLOAD": value}, config)
    assert runtime.embedding.fastembed.allow_download is expected
