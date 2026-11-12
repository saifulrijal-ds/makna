"""WP-01 criterion 5 and the settings loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from makna.config import ConfigError, Settings, load_search_runtime, load_settings
from makna.model.governance import RULES, GovernancePolicy

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


def test_a_minimum_group_size_of_one_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "makna.toml"
    config.write_text("[governance]\nmin_group_size = 1\n")
    with pytest.raises(ConfigError, match="min_group_size"):
        load_settings(GOOD_ENV, config)


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


# ---- governance modes (WP-10B criterion 5, D-29) -------------------------------------------


def governance(tmp_path: Path, body: str) -> Settings:
    config = tmp_path / "makna.toml"
    config.write_text(f"[governance]\n{body}\n")
    return load_settings(GOOD_ENV, config)


def refused(tmp_path: Path, body: str, field: str) -> str:
    with pytest.raises(ConfigError) as caught:
        governance(tmp_path, body)
    message = str(caught.value)
    assert f"governance.{field}" in message, message
    return message


def test_the_default_mode_is_strict_with_a_minimum_of_ten(tmp_path: Path) -> None:
    settings = load_settings(GOOD_ENV, tmp_path / "none.toml")
    assert settings.governance.mode == "strict"
    assert settings.governance.policy() == GovernancePolicy.strict(10)


def test_the_committed_config_file_is_strict(tmp_path: Path) -> None:
    settings = load_settings(GOOD_ENV, ROOT / "config" / "makna.toml")
    assert settings.governance.policy() == GovernancePolicy.strict(10)
    assert settings.governance.team == "data-governance"


def test_strict_takes_a_minimum(tmp_path: Path) -> None:
    settings = governance(tmp_path, 'mode = "strict"\nmin_group_size = 25')
    assert settings.governance.policy() == GovernancePolicy.strict(25)


def test_off_gives_the_off_policy_and_ignores_the_minimum(tmp_path: Path) -> None:
    assert governance(tmp_path, 'mode = "off"').governance.policy() == GovernancePolicy.off()
    ignored = governance(tmp_path, 'mode = "off"\nmin_group_size = 25')
    assert ignored.governance.policy() == GovernancePolicy.off()


def test_custom_defaults_every_rule_to_its_strict_value(tmp_path: Path) -> None:
    settings = governance(tmp_path, 'mode = "custom"')
    assert settings.governance.policy() == GovernancePolicy.custom()
    assert settings.governance.policy().relaxed() == ()


def test_custom_sets_each_rule(tmp_path: Path) -> None:
    body = (
        'mode = "custom"\nmin_group_size = 0\nallow_personal_filters = true\n'
        "apply_masks = false\nallow_unmasked_personal = true"
    )
    policy = governance(tmp_path, body).governance.policy()
    assert policy == GovernancePolicy.custom(
        min_group_size=0,
        allow_personal_filters=True,
        apply_masks=False,
        allow_unmasked_personal=True,
    )
    assert [name for name, _ in policy.relaxed()] == list(RULES)


@pytest.mark.parametrize("value", ["tight", "STRICT", "", "relaxed"])
def test_an_unknown_mode_is_refused_and_names_the_field(tmp_path: Path, value: str) -> None:
    refused(tmp_path, f'mode = "{value}"', "mode")


@pytest.mark.parametrize("mode", ["strict", "off"])
@pytest.mark.parametrize(
    "rule", ["allow_personal_filters", "apply_masks", "allow_unmasked_personal"]
)
def test_a_custom_rule_outside_custom_is_refused_even_at_its_strict_value(
    tmp_path: Path, mode: str, rule: str
) -> None:
    strict_value = "true" if rule == "apply_masks" else "false"
    for value in ("true", "false", strict_value):
        message = refused(tmp_path, f'mode = "{mode}"\n{rule} = {value}', rule)
        assert "custom" in message


def test_a_custom_rule_with_no_mode_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, "apply_masks = false", "apply_masks")


@pytest.mark.parametrize("mode", ["strict", "custom", "off"])
@pytest.mark.parametrize("bad", [1, -1, -10])
def test_a_minimum_of_one_or_less_than_zero_is_refused_in_every_mode(
    tmp_path: Path, mode: str, bad: int
) -> None:
    refused(tmp_path, f'mode = "{mode}"\nmin_group_size = {bad}', "min_group_size")


@pytest.mark.parametrize("mode", ["strict", "off"])
def test_a_minimum_of_zero_is_accepted_only_in_custom(tmp_path: Path, mode: str) -> None:
    message = refused(tmp_path, f'mode = "{mode}"\nmin_group_size = 0', "min_group_size")
    assert "custom" in message
    refused(tmp_path, "min_group_size = 0", "min_group_size")  # no mode: strict


def test_a_minimum_of_zero_in_custom_turns_the_rule_off(tmp_path: Path) -> None:
    policy = governance(tmp_path, 'mode = "custom"\nmin_group_size = 0').governance.policy()
    assert policy == GovernancePolicy.custom(min_group_size=0)
    assert policy.relaxed() == (("min_group_size", 0),)


def test_an_unknown_governance_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="governance.allow_everything"):
        governance(tmp_path, 'mode = "custom"\nallow_everything = true')


def test_the_environment_cannot_set_the_mode(tmp_path: Path) -> None:
    env = {**GOOD_ENV, "MAKNA_GOVERNANCE_MODE": "off", "MAKNA_MODE": "off"}
    assert load_settings(env, tmp_path / "none.toml").governance.mode == "strict"


# ---- fix round 1: typed values, immutable settings, hints -------------------------------------


@pytest.mark.parametrize("value", ['"no"', '"yes"', '"false"', "0", "1", '"on"'])
@pytest.mark.parametrize(
    "rule", ["allow_personal_filters", "apply_masks", "allow_unmasked_personal"]
)
def test_a_custom_rule_must_be_a_real_boolean(tmp_path: Path, value: str, rule: str) -> None:
    refused(tmp_path, f'mode = "custom"\n{rule} = {value}', rule)


@pytest.mark.parametrize("value", ['"10"', "10.0", "true", "false"])
def test_the_minimum_must_be_a_real_integer(tmp_path: Path, value: str) -> None:
    refused(tmp_path, f'mode = "custom"\nmin_group_size = {value}', "min_group_size")


def test_the_governance_settings_cannot_be_changed_after_loading(tmp_path: Path) -> None:
    settings = governance(tmp_path, 'mode = "strict"')
    with pytest.raises(ValidationError):
        settings.governance.mode = "off"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        settings.governance.apply_masks = False  # type: ignore[misc]
    assert settings.governance.policy() == GovernancePolicy.strict(10)


def test_the_minimum_hint_about_zero_appears_only_in_custom(tmp_path: Path) -> None:
    assert "or 0" not in refused(tmp_path, 'mode = "strict"\nmin_group_size = 1', "min_group_size")
    assert "or 0" not in refused(tmp_path, 'mode = "off"\nmin_group_size = 1', "min_group_size")
    assert "or 0" in refused(tmp_path, 'mode = "custom"\nmin_group_size = 1', "min_group_size")
