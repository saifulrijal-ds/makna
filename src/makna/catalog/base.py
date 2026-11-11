"""The connector interface (ARCHITECTURE section 6.1)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from makna.model.catalog import CatalogDataset, CatalogGlossaryTerm


class ConnectorError(Exception):
    """A connector is misconfigured or its source is unreadable; the message says what is wrong."""


class CatalogConnector(Protocol):
    """What a data catalog must provide. A connector never writes to the catalog."""

    @property
    def name(self) -> str:
        """Name of the catalog source, as stored in `catalog_source.name`."""
        ...

    @property
    def kind(self) -> str:
        """Connector kind, as stored in `catalog_source.connector` (for example `file`)."""
        ...

    def list_datasets(self) -> Iterator[CatalogDataset]: ...

    def list_glossary_terms(self) -> Iterator[CatalogGlossaryTerm]: ...

    def check(self) -> None:
        """Raises `ConnectorError` with a clear message if the connector cannot be used."""
