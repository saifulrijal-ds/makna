"""Run the Alembic migrations from Python (used by `init` and the tests)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

SCRIPT_LOCATION = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    config.attributes["url"] = url
    return config


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)
