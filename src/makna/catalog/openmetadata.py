"""The OpenMetadata connector (ARCHITECTURE section 6.1, CON-2).

Reads tables with their columns, tags and owners, and glossary terms, from OpenMetadata's REST
API with `httpx`. The OpenMetadata Python SDK is not used: it is not under a permissive licence
(`docs/notes/openmetadata-sdk-license.md`). The connector only reads (`GET`).

Safety rules, each tested in `tests/unit/test_catalog_openmetadata.py`:

- The token reaches the connector as a `SecretStr`, is unwrapped only to build the header of one
  request, and is in no message, no `repr` and no log line.
- `base_url` must be `https://`, except `http://localhost`, `http://127.0.0.1` and
  `http://[::1]`, so the bearer token never travels in clear to another host. Redirects are
  never followed: a redirect would send the token to the host the server names.
- Every failure is a `ConnectorError` with a fixed text per kind. A text names the endpoint
  path and the status at most; a response body is never quoted, because it can hold names and
  data.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, ValidationError
from pydantic.alias_generators import to_camel

from makna.catalog.base import ConnectorError
from makna.model.catalog import (
    CatalogColumn,
    CatalogDataset,
    CatalogGlossaryTerm,
    CatalogOwner,
    DataType,
)

TABLES_PATH = "/api/v1/tables"
GLOSSARY_TERMS_PATH = "/api/v1/glossaryTerms"
TABLE_FIELDS = "columns,tableConstraints,tags,owners"
GLOSSARY_TERM_FIELDS = "relatedTerms,tags"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
MAX_BODY_BYTES = 64 * 1024 * 1024
"""The largest answer read: one page of tables with their columns. A larger body is an error."""
DEADLINE_FACTOR = 4
"""A request, headers and body together, may take this many times `timeout` (the library's
timeout is per operation, so a server that sends one byte at a time would never trip it)."""

# What a JWT or an opaque token is made of. A header value with a newline or a non-ASCII
# character would be refused by the HTTP library with a message that quotes the value.
_TOKEN_CHARACTERS = re.compile(r"[A-Za-z0-9._~+/=-]+")

# OpenMetadata's column data types (`entity/data/table.json`, `dataType`) to the normalized types
# of ARCHITECTURE section 6.1. A type that is not listed is `other`.
_DATA_TYPES: dict[str, DataType] = {
    **dict.fromkeys(("STRING", "VARCHAR", "CHAR", "TEXT", "MEDIUMTEXT", "NTEXT", "UUID"), "string"),
    **dict.fromkeys(
        ("TINYINT", "SMALLINT", "INT", "BIGINT", "BYTEINT", "LARGEINT", "UINT"), "integer"
    ),
    **dict.fromkeys(("DECIMAL", "NUMERIC", "NUMBER", "MONEY"), "decimal"),
    **dict.fromkeys(("FLOAT", "DOUBLE"), "float"),
    "BOOLEAN": "boolean",
    "DATE": "date",
    **dict.fromkeys(("TIMESTAMP", "TIMESTAMPZ", "DATETIME"), "timestamp"),
}


def check_base_url(url: str) -> str:
    """The base URL without a trailing slash; raises `ConnectorError` if it is not safe to use.

    The rule is the one of the stdio bridge's `MAKNA_URL`: `https://`, or `http://` for
    localhost only, and no user name or password in the URL. The message never shows the URL.
    """
    text = url.strip()
    if re.search(r"[\x00-\x20\x7f]", text):
        raise ConnectorError("OpenMetadata base_url is not a URL")
    if not text.lower().startswith(("http://", "https://")):
        raise ConnectorError("OpenMetadata base_url must start with https:// (or http://localhost)")
    try:
        parts = urlsplit(text)
        host, user, password = parts.hostname, parts.username, parts.password
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        raise ConnectorError("OpenMetadata base_url is not a URL") from None
    if not host:
        raise ConnectorError("OpenMetadata base_url is not a URL")
    if user is not None or password is not None:
        raise ConnectorError("OpenMetadata base_url must not hold a user name or password")
    if parts.scheme.lower() == "http" and host.lower() not in LOCAL_HOSTS:
        raise ConnectorError(
            "OpenMetadata base_url must use https:// (http:// is for localhost only), "
            "because the token is sent with every request"
        )
    if parts.query or parts.fragment:
        raise ConnectorError("OpenMetadata base_url must not have a query or a fragment")
    return text.rstrip("/")


class _Lenient(BaseModel):
    """A reader of the parts of an OpenMetadata answer that Makna uses; the rest is ignored."""

    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)


class _Reference(_Lenient):
    id: str | None = None
    type: str | None = None
    name: str | None = None
    fully_qualified_name: str | None = None


class _TagLabel(_Lenient):
    tag_fqn: str = Field(alias="tagFQN")
    state: str = "Confirmed"


class _Column(_Lenient):
    name: str
    data_type: str
    data_type_display: str | None = None
    description: str | None = None
    constraint: str | None = None
    tags: list[_TagLabel] = []


class _TableConstraint(_Lenient):
    constraint_type: str | None = None
    columns: list[str] = []


class _Table(_Lenient):
    id: str
    name: str
    fully_qualified_name: str | None = None
    description: str | None = None
    updated_at: StrictInt | None = None
    columns: list[_Column] = []
    table_constraints: list[_TableConstraint] = []
    owners: list[_Reference] = []
    tags: list[_TagLabel] = []
    service: _Reference | None = None
    database_schema: _Reference | None = None


class _RelatedTerm(_Lenient):
    term: _Reference


class _GlossaryTerm(_Lenient):
    id: str
    name: str
    description: str | None = None
    synonyms: list[str] = []
    related_terms: list[_RelatedTerm] = []
    tags: list[_TagLabel] = []


class _Paging(_Lenient):
    after: str | None = None


class _Page(_Lenient):
    data: list[dict[str, Any]]
    paging: _Paging = _Paging()


def _confirmed_tags(labels: list[_TagLabel]) -> list[str]:
    """Tag names of the labels a person or a tool has confirmed, each once, in order."""
    return list(dict.fromkeys(label.tag_fqn for label in labels if label.state == "Confirmed"))


class OpenMetadataConnector:
    """Catalog source backed by an OpenMetadata server.

    `services` maps an OpenMetadata database service name to the catalog name of the query
    engine: the four-part name `service.database.schema.table` becomes
    `<services[service]>.<schema>.<table>`. The database part is dropped, because the engine
    reads one database; two tables of one service with the same schema and table name in
    different databases are an error, not a guess. A table of a service that is not in
    `services` is skipped and counted in `skipped_services`.
    """

    kind = "openmetadata"

    def __init__(
        self,
        base_url: str,
        token: SecretStr,
        *,
        services: Mapping[str, str],
        source_name: str = "openmetadata",
        page_size: int = 100,
        max_pages: int = 1000,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = check_base_url(base_url)
        if _TOKEN_CHARACTERS.fullmatch(token.get_secret_value()) is None:
            raise ConnectorError("the OpenMetadata token holds a character a token never has")
        self._token = token
        self._services = dict(services)
        self._name = source_name
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout
        self.skipped_services: dict[str, int] = {}
        """Service name to the number of tables skipped in the last `list_datasets` run."""
        self.skipped_tables: list[str] = []
        """`service.schema.table` of each table the common model cannot hold, skipped in the last
        run. Names of catalog objects, not data."""
        # No redirects: a redirect would carry the token to the host the server names.
        self._client = httpx.Client(
            base_url=self._base_url, timeout=timeout, follow_redirects=False, transport=transport
        )

    def __repr__(self) -> str:
        return f"OpenMetadataConnector(name={self._name!r})"

    @property
    def name(self) -> str:
        return self._name

    def close(self) -> None:
        self._client.close()

    # --- HTTP ---

    def _get(self, path: str, params: dict[str, str | int]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
        }
        deadline = time.monotonic() + DEADLINE_FACTOR * self._timeout
        try:
            with self._client.stream("GET", path, params=params, headers=headers) as response:
                status = response.status_code
                if status != 200:
                    content = b""
                else:
                    content = self._read(response, path, deadline)
        except httpx.TimeoutException:
            raise ConnectorError(
                f"OpenMetadata did not answer {path} within {self._timeout:g} seconds"
            ) from None
        except httpx.HTTPError:
            raise ConnectorError(f"OpenMetadata cannot be reached at {path}") from None
        if status in (401, 403):
            raise ConnectorError(
                f"OpenMetadata refused the token at {path} (HTTP {status}); "
                "check MAKNA_OPENMETADATA_TOKEN"
            )
        if status != 200:
            raise ConnectorError(f"OpenMetadata answered {path} with HTTP {status}")
        try:
            body = json.loads(content)
        except (ValueError, RecursionError):
            raise ConnectorError(
                f"OpenMetadata answered {path} with a body that is not JSON"
            ) from None
        if not isinstance(body, dict):
            raise ConnectorError(f"OpenMetadata answered {path} in a shape Makna cannot read")
        return body

    def _read(self, response: httpx.Response, path: str, deadline: float) -> bytes:
        """The body, up to `MAX_BODY_BYTES` and the overall deadline."""
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ConnectorError(
                    f"OpenMetadata answered {path} with more than {MAX_BODY_BYTES} bytes"
                )
            if time.monotonic() > deadline:
                raise ConnectorError(
                    f"OpenMetadata did not finish answering {path} within "
                    f"{DEADLINE_FACTOR * self._timeout:g} seconds"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _pages(self, path: str, fields: str) -> Iterator[list[dict[str, Any]]]:
        """The items of each page of a listing, following the `after` cursor."""
        params: dict[str, str | int] = {"fields": fields, "limit": self._page_size}
        seen: set[str] = set()
        for _ in range(self._max_pages):
            body = self._get(path, params)
            try:
                page = _Page.model_validate(body)
            except ValidationError:
                raise ConnectorError(
                    f"OpenMetadata answered {path} in a shape Makna cannot read"
                ) from None
            yield page.data
            cursor = page.paging.after
            if not cursor:
                return
            if cursor in seen:
                raise ConnectorError(f"OpenMetadata repeated a page cursor at {path}")
            seen.add(cursor)
            params = {**params, "after": cursor}
        raise ConnectorError(
            f"OpenMetadata listing {path} has more than {self._max_pages} pages; "
            "raise max_pages in [catalog.openmetadata] if that is expected"
        )

    def _items(self, path: str, fields: str) -> Iterator[dict[str, Any]]:
        for page in self._pages(path, fields):
            yield from page

    # --- the interface ---

    def check(self) -> None:
        """One authenticated request: a wrong token or URL fails here, before anything is read."""
        self._get(TABLES_PATH, {"limit": 1})

    def list_datasets(self) -> Iterator[CatalogDataset]:
        self.skipped_services = {}
        self.skipped_tables = []
        taken: dict[str, str] = {}
        for raw in self._items(TABLES_PATH, TABLE_FIELDS):
            try:
                table = _Table.model_validate(raw)
            except ValidationError:
                raise ConnectorError(
                    f"OpenMetadata answered {TABLES_PATH} in a shape Makna cannot read"
                ) from None
            service, schema = self._service_and_schema(table)
            catalog = self._services.get(service)
            if catalog is None:
                self.skipped_services[service] = self.skipped_services.get(service, 0) + 1
                continue
            engine_fqn = f"{catalog}.{schema}.{table.name}"
            if engine_fqn in taken:
                raise ConnectorError(
                    f"two tables of OpenMetadata service {service!r} map to the same "
                    "engine_fqn (the same schema and table name in two databases)"
                )
            try:
                dataset = self._dataset(table, engine_fqn)
            except ValidationError:
                # A name the common model cannot hold (a dot, a space, an empty or repeated
                # name): the table is skipped and reported by name, not guessed and not fatal.
                self.skipped_tables.append(f"{service}.{schema}.{table.name}")
                continue
            taken[engine_fqn] = table.id
            yield dataset

    def list_glossary_terms(self) -> Iterator[CatalogGlossaryTerm]:
        for raw in self._items(GLOSSARY_TERMS_PATH, GLOSSARY_TERM_FIELDS):
            try:
                term = _GlossaryTerm.model_validate(raw)
            except ValidationError:
                raise ConnectorError(
                    f"OpenMetadata answered {GLOSSARY_TERMS_PATH} in a shape Makna cannot read"
                ) from None
            related = [r.term.name or r.term.fully_qualified_name for r in term.related_terms]
            try:
                mapped = CatalogGlossaryTerm(
                    catalog_id=term.id,
                    name=term.name,
                    definition=term.description or "",
                    synonyms=[s for s in term.synonyms if s],
                    related_terms=[name for name in related if name],
                    tags=_confirmed_tags(term.tags),
                )
            except ValidationError:
                raise ConnectorError(
                    f"OpenMetadata answered {GLOSSARY_TERMS_PATH} with a term that has no "
                    "name or no id"
                ) from None
            yield mapped

    # --- mapping ---

    @staticmethod
    def _service_and_schema(table: _Table) -> tuple[str, str]:
        """The service and schema names, from the references, else from the four-part name."""
        service = table.service.name if table.service else None
        schema = table.database_schema.name if table.database_schema else None
        if not service or not schema:
            parts = (table.fully_qualified_name or "").split(".")
            if len(parts) != 4:
                raise ConnectorError(
                    f"OpenMetadata answered {TABLES_PATH} in a shape Makna cannot read"
                )
            service, schema = parts[0], parts[2]
        return service, schema

    @staticmethod
    def _dataset(table: _Table, engine_fqn: str) -> CatalogDataset:
        primary_key = {
            name
            for constraint in table.table_constraints
            if constraint.constraint_type == "PRIMARY_KEY"
            for name in constraint.columns
        }
        columns = []
        for position, column in enumerate(table.columns):
            is_key = column.constraint == "PRIMARY_KEY" or column.name in primary_key
            columns.append(
                CatalogColumn(
                    name=column.name,
                    data_type=_DATA_TYPES.get(column.data_type.upper(), "other"),
                    native_type=column.data_type_display or column.data_type.lower(),
                    description=column.description or "",
                    tags=_confirmed_tags(column.tags),
                    nullable=not (is_key or column.constraint == "NOT_NULL"),
                    is_primary_key=is_key,
                    ordinal=position,
                )
            )
        owners = []
        for owner in table.owners:
            name = owner.name or owner.fully_qualified_name
            if name and owner.type == "user":
                owners.append(CatalogOwner(name=name, type="user"))
            elif name and owner.type == "team":
                owners.append(CatalogOwner(name=name, type="team"))
        updated_at = None
        if table.updated_at is not None:
            try:
                updated_at = datetime.fromtimestamp(table.updated_at / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                raise ConnectorError(
                    f"OpenMetadata answered {TABLES_PATH} with a date Makna cannot read"
                ) from None
        return CatalogDataset(
            engine_fqn=engine_fqn,
            catalog_id=table.id,
            name=table.name,
            description=table.description or "",
            owners=owners,
            tags=_confirmed_tags(table.tags),
            columns=columns,
            updated_at=updated_at,
        )
