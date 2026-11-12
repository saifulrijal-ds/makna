"""Settings: connection settings and secrets from environment variables, the rest from TOML.

Secrets and connection settings come only from the environment (NFR-4). Non-secret settings
(search weights, tag map, dialect, governance) come from `config/makna.toml`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    ValidationError,
    ValidationInfo,
    field_validator,
)

from makna.model.governance import (
    DEFAULT_MIN_GROUP_SIZE,
    MIN_GROUP_SIZE_FLOOR,
    GovernanceMode,
    GovernancePolicy,
)
from makna.model.search import EmbeddingSettings, SearchSettings

ENV_PREFIX = "MAKNA_"
DEFAULT_CONFIG_PATH = Path("config/makna.toml")

# Environment variables that must be set. Field name -> variable name is ENV_PREFIX + upper().
REQUIRED_VARIABLES = ("MAKNA_DATABASE_URL", "MAKNA_SECRET_KEY")

EmbeddingProvider = Literal["fastembed", "ollama", "openai_compatible", "hash"]


class ConfigError(Exception):
    """Settings could not be loaded. The message names the variable or file at fault."""


class GovernanceSettings(BaseModel):
    """`[governance]` of `config/makna.toml` (D-29).

    `mode` is `strict` (the default), `custom`, or `off`, written in lower case. Booleans and the
    minimum must be real TOML booleans and integers (a string or 1 for true is an error), and
    the settings cannot be changed after loading. The three booleans are rules of `custom`
    only: setting one in any other mode is an error, because a rule that is silently ignored
    would look like protection. `min_group_size` may be set in `strict` (at least 2) and in
    `custom` (0 turns the rule off, otherwise at least 2). In `off` it is ignored (the rule is
    off), but the same limits are checked: 1 and negative numbers are always errors, and 0 is
    accepted only in `custom`. `policy()` builds the `GovernancePolicy` the compiler is given.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    team: str = "data-governance"
    mode: GovernanceMode = "strict"
    min_group_size: StrictInt = DEFAULT_MIN_GROUP_SIZE
    allow_personal_filters: StrictBool | None = None
    apply_masks: StrictBool | None = None
    allow_unmasked_personal: StrictBool | None = None

    @field_validator("min_group_size")
    @classmethod
    def _check_minimum(cls, value: int, info: ValidationInfo) -> int:
        if value < 0 or value == 1:
            custom = info.data.get("mode") == "custom"
            raise ValueError(
                f"{value} is not allowed: use at least {MIN_GROUP_SIZE_FLOOR}, because a group of "
                "one is a single record" + (" (or 0 to turn the rule off)" if custom else "")
            )
        if value == 0 and info.data.get("mode", "strict") != "custom":
            raise ValueError(
                "0 (rule off) is allowed only in mode custom; use at least "
                f'{MIN_GROUP_SIZE_FLOOR}, or set mode = "custom"'
            )
        return value

    @field_validator("allow_personal_filters", "apply_masks", "allow_unmasked_personal")
    @classmethod
    def _custom_only(cls, value: bool | None, info: ValidationInfo) -> bool | None:
        mode = info.data.get("mode", "strict")
        if value is not None and mode != "custom":
            raise ValueError(
                f"is a rule of mode custom and cannot be set in mode {mode}; set mode = "
                '"custom" or remove it'
            )
        return value

    def policy(self) -> GovernancePolicy:
        """The policy of this configuration."""
        if self.mode == "off":
            return GovernancePolicy.off()
        if self.mode == "strict":
            return GovernancePolicy.strict(self.min_group_size)
        return GovernancePolicy.custom(
            min_group_size=self.min_group_size,
            allow_personal_filters=bool(self.allow_personal_filters),
            apply_masks=True if self.apply_masks is None else self.apply_masks,
            allow_unmasked_personal=bool(self.allow_unmasked_personal),
        )


class SensitivitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_map: dict[str, str] = Field(
        default_factory=lambda: {
            "PII.Sensitive": "personal",
            "PersonalData.Personal": "personal",
            "PII.NonSensitive": "confidential",
        }
    )


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # From the environment (secrets and connection settings).
    database_url: SecretStr
    secret_key: SecretStr
    definitions_repo: str | None = None
    engine_url: SecretStr | None = None
    embedding_provider: EmbeddingProvider = "fastembed"
    embedding_api_key: SecretStr | None = None
    openmetadata_token: SecretStr | None = None
    # Two-person approval is on unless this is true (D-11).
    allow_self_approval: bool = False

    # From config/makna.toml.
    dialect: str = "postgres"
    search: SearchSettings = Field(default_factory=SearchSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    sensitivity: SensitivitySettings = Field(default_factory=SensitivitySettings)


_ENV_FIELDS = (
    "database_url",
    "secret_key",
    "definitions_repo",
    "engine_url",
    "embedding_provider",
    "embedding_api_key",
    "openmetadata_token",
    "allow_self_approval",
)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error


def _collect_values(env: Mapping[str, str], config_path: Path | str | None) -> dict[str, Any]:
    """The TOML file's values with the environment's overrides applied."""
    values: dict[str, Any] = _read_toml(Path(config_path or DEFAULT_CONFIG_PATH))
    for field in _ENV_FIELDS:
        value = env.get(ENV_PREFIX + field.upper())
        if value:
            values[field] = value
    cache_dir = env.get(ENV_PREFIX + "EMBEDDING_CACHE_DIR")
    if cache_dir:
        embedding = dict(values.get("embedding") or {})
        embedding["fastembed"] = {**(embedding.get("fastembed") or {}), "cache_dir": cache_dir}
        values["embedding"] = embedding
    allow = env.get(ENV_PREFIX + "EMBEDDING_ALLOW_DOWNLOAD")
    if allow:
        embedding = dict(values.get("embedding") or {})
        embedding["fastembed"] = {**(embedding.get("fastembed") or {}), "allow_download": allow}
        values["embedding"] = embedding
    return values


def _problems(error: ValidationError) -> str:
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        name = ENV_PREFIX + where.upper() if where in _ENV_FIELDS else where
        problems.append(f"{name}: {item['msg']}")
    return "Invalid settings: " + "; ".join(problems)


def load_settings(
    env: Mapping[str, str] | None = None, config_path: Path | str | None = None
) -> Settings:
    """Load settings. Raises ConfigError naming the variable when a required one is missing.

    `env` defaults to `os.environ`. A missing config file means defaults; an empty variable
    counts as missing.
    """
    if env is None:
        import os

        env = os.environ

    missing = [name for name in REQUIRED_VARIABLES if not env.get(name)]
    if missing:
        raise ConfigError("Missing required environment variable(s): " + ", ".join(missing))

    try:
        return Settings.model_validate(_collect_values(env, config_path))
    except ValidationError as error:
        raise ConfigError(_problems(error)) from error


class SearchRuntime(BaseModel):
    """What the indexer and the search need: the provider choice and the search weights.

    Operator commands (`makna index build`, `makna search`) use this, so they do not require
    `MAKNA_SECRET_KEY`.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    embedding_provider: EmbeddingProvider = "fastembed"
    embedding_api_key: SecretStr | None = None
    search: SearchSettings = Field(default_factory=SearchSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)


def load_search_runtime(
    env: Mapping[str, str] | None = None, config_path: Path | str | None = None
) -> SearchRuntime:
    """The search settings of `config/makna.toml` and the embedding variables of the environment."""
    if env is None:
        import os

        env = os.environ
    try:
        return SearchRuntime.model_validate(_collect_values(env, config_path))
    except ValidationError as error:
        raise ConfigError(_problems(error)) from error


def load_tag_map(config_path: Path | str | None = None) -> dict[str, str]:
    """The catalog-tag to sensitivity mapping (`[sensitivity.tag_map]`) without the secrets.

    Operator commands that only need the mapping (`makna catalog sync`) use this, so they do not
    require `MAKNA_SECRET_KEY`. A missing config file means the default mapping.
    """
    values = _read_toml(Path(config_path or DEFAULT_CONFIG_PATH))
    try:
        return dict(SensitivitySettings.model_validate(values.get("sensitivity", {})).tag_map)
    except ValidationError as error:
        raise ConfigError(f"Invalid settings: sensitivity: {error}") from error
