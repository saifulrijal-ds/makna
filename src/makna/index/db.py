"""Database connection helpers for the Makna application database."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


def sqlalchemy_url(url: str) -> str:
    """Return `url` with the psycopg 3 driver named, so `postgresql://...` works as given."""
    parsed = make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def database_url_from_env() -> str:
    """The application database URL from `MAKNA_DATABASE_URL`; never from a committed value."""
    url = os.environ.get("MAKNA_DATABASE_URL")
    if not url:
        raise RuntimeError("Missing required environment variable: MAKNA_DATABASE_URL")
    return url


def make_engine(url: str) -> Engine:
    return create_engine(sqlalchemy_url(url))
