"""The engine interface (ARCHITECTURE section 6.3) and what every connector shares.

Makna talks to the target engine only for its own needs: listing tables and columns, and the
freshness check. It never runs a caller's query or SQL (D-21).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlglot import exp

_URL_SCHEMES = ("postgresql://", "postgres://")


class EngineError(Exception):
    """The engine cannot be read; the message says what is wrong.

    The message never holds the service account's password (`redact_url`, `scrub`).
    """


@dataclass(frozen=True)
class EngineColumn:
    """A column as the engine reports it. `native_type` is the engine's own type name."""

    name: str
    native_type: str


@dataclass(frozen=True)
class EngineTable:
    """A table of the engine with its columns in table order."""

    schema: str
    name: str
    columns: tuple[EngineColumn, ...]


class EngineConnector(Protocol):
    """What a target engine must provide. A connector never runs a caller's SQL."""

    @property
    def dialect(self) -> str:
        """The sqlglot dialect name: `postgres` in the MVP (`duckdb` appears in tests only)."""
        ...

    def list_tables(self, schema: str) -> list[EngineTable]:
        """The base tables of `schema` with their columns."""
        ...

    def latest_value(self, table: str, expr: exp.Expr) -> date | None:
        """`SELECT MAX(expr) FROM table`: the freshness check. None if the table has no rows
        or the expression is NULL everywhere."""
        ...


def redact_url(url: str) -> str:
    """The connection URL without its password, safe to print and to log.

    The password is removed from the user information and from a `password` query parameter.
    A value that is not a `postgresql://` URL (a libpq key/value string) is rendered as
    `key=value` pairs without `password`. Anything that cannot be read is replaced by a
    placeholder, never returned as it was.
    """
    try:
        if url.startswith(_URL_SCHEMES):
            parts = urlsplit(url)
            host = parts.netloc.rpartition("@")[2]
            user = parts.netloc.rpartition("@")[0].partition(":")[0]
            query = urlencode(
                [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != "password"],
                safe="/",
            )
            netloc = f"{user}@{host}" if user else host
            return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
        from psycopg.conninfo import conninfo_to_dict

        pairs = conninfo_to_dict(url)
        return " ".join(f"{k}={v}" for k, v in pairs.items() if k != "password")
    except Exception:  # a malformed value must not leak through the error path
        return "<unreadable connection setting>"


def password_of(url: str) -> str | None:
    """The password inside a connection URL or key/value string, if there is one."""
    try:
        from psycopg.conninfo import conninfo_to_dict

        value = conninfo_to_dict(url).get("password")
    except Exception:
        return None
    return str(value) if value else None


def scrub(text: str, url: str) -> str:
    """`text` with every occurrence of the URL's password replaced, for a message that holds
    text from a library."""
    password = password_of(url)
    return text.replace(password, "***") if password else text
