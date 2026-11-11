"""Settings: connection settings and secrets from environment variables, the rest from TOML.

Secrets and connection settings come only from the environment (NFR-4). Non-secret settings
(search weights, tag map, dialect, governance) come from `config/makna.toml`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

ENV_PREFIX = "MAKNA_"
DEFAULT_CONFIG_PATH = Path("config/makna.toml")

# Environment variables that must be set. Field name -> variable name is ENV_PREFIX + upper().
REQUIRED_VARIABLES = ("MAKNA_DATABASE_URL", "MAKNA_SECRET_KEY")

EmbeddingProvider = Literal["fastembed", "ollama", "openai_compatible", "hash"]


class ConfigError(Exception):
    """Settings could not be loaded. The message names the variable or file at fault."""


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    w_keyword: float = 1.0
    w_vector: float = 1.0


class GovernanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: str = "data-governance"
    min_group_size: int = Field(default=10, ge=1)


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
    openmetadata_token: SecretStr | None = None
    # Two-person approval is on unless this is true (D-11).
    allow_self_approval: bool = False

    # From config/makna.toml.
    dialect: str = "postgres"
    search: SearchSettings = Field(default_factory=SearchSettings)
    governance: GovernanceSettings = Field(default_factory=GovernanceSettings)
    sensitivity: SensitivitySettings = Field(default_factory=SensitivitySettings)


_ENV_FIELDS = (
    "database_url",
    "secret_key",
    "definitions_repo",
    "engine_url",
    "embedding_provider",
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

    values: dict[str, Any] = _read_toml(Path(config_path or DEFAULT_CONFIG_PATH))
    for field in _ENV_FIELDS:
        value = env.get(ENV_PREFIX + field.upper())
        if value:
            values[field] = value

    try:
        return Settings.model_validate(values)
    except ValidationError as error:
        problems = []
        for item in error.errors():
            where = ".".join(str(part) for part in item["loc"])
            name = ENV_PREFIX + where.upper() if where in _ENV_FIELDS else where
            problems.append(f"{name}: {item['msg']}")
        raise ConfigError("Invalid settings: " + "; ".join(problems)) from error


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
