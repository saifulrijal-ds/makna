"""Alembic environment: the URL comes from MAKNA_DATABASE_URL (or a caller of `migrate`)."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from makna.index.db import database_url_from_env, sqlalchemy_url
from makna.index.tables import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> str:
    given = config.attributes.get("url")
    return sqlalchemy_url(given if isinstance(given, str) else database_url_from_env())


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported; set MAKNA_DATABASE_URL.")
run_migrations_online()
