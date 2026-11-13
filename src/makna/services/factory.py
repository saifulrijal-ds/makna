"""Building the services from settings: the one place a database engine, an embedding provider,
and the governance policy meet. `makna.app` calls this and keeps the result on `app.state`."""

from __future__ import annotations

from makna.config import ConfigError, Settings
from makna.index.db import make_engine
from makna.search.embedding import EmbeddingError, make_provider
from makna.services.service import Services


def build_services(settings: Settings) -> Services:
    """The services of `settings`. Nothing connects to the database until the first call.

    Raises `ConfigError` for a provider or dialect that cannot be built, so the server fails at
    start and not at the first request.
    """
    key = settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None
    try:
        provider = make_provider(settings.embedding_provider, settings.embedding, key)
        return Services(
            make_engine(settings.database_url.get_secret_value()),
            provider,
            policy=settings.governance.policy(),
            dialect=settings.dialect,
            search_settings=settings.search,
        )
    except (EmbeddingError, ValueError) as error:
        raise ConfigError(f"Invalid settings: {error}") from error
