"""The file connector: reads a `makna-catalog/v1` file (ARCHITECTURE section 6.2, CON-3).

The file is YAML or JSON. It is read once and validated as a whole, so a file that does not
match the format is rejected before anything is synced, with an error that names the field.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from makna.catalog.base import ConnectorError
from makna.model.catalog import CatalogDataset, CatalogGlossaryTerm

FORMAT = "makna-catalog/v1"


class CatalogSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    exported_at: str | None = None

    @field_validator("name")
    @classmethod
    def _named(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("exported_at", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return value.isoformat() if hasattr(value, "isoformat") else value


class CatalogFile(BaseModel):
    """The whole file."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["makna-catalog/v1"]
    source: CatalogSource
    datasets: list[CatalogDataset] = []
    glossary_terms: list[CatalogGlossaryTerm] = []

    @model_validator(mode="after")
    def _unique_keys(self) -> CatalogFile:
        seen: set[str] = set()
        for index, dataset in enumerate(self.datasets):
            if dataset.engine_fqn in seen:
                raise ValueError(
                    f"datasets[{index}].engine_fqn: {dataset.engine_fqn!r} appears twice"
                )
            seen.add(dataset.engine_fqn)
        ids: set[str] = set()
        for index, term in enumerate(self.glossary_terms):
            if term.catalog_id in ids:
                raise ValueError(
                    f"glossary_terms[{index}].catalog_id: {term.catalog_id!r} appears twice"
                )
            ids.add(term.catalog_id)
        return self


def _field_path(location: tuple[int | str, ...]) -> str:
    path = ""
    for step in location:
        path += f"[{step}]" if isinstance(step, int) else (f".{step}" if path else str(step))
    return path


def parse_catalog(text: str, origin: str = "catalog file") -> CatalogFile:
    """Decode and validate the text of a catalog file. Raises `ConnectorError` naming the field."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or str(error)
        raise ConnectorError(f"{origin}: not readable as YAML or JSON: {problem}") from None
    if not isinstance(data, dict):
        raise ConnectorError(f"{origin}: the top level must be a mapping with a `format` key")
    if data.get("format") != FORMAT:
        raise ConnectorError(f"{origin}: format: expected {FORMAT!r}, found {data.get('format')!r}")
    try:
        parsed = CatalogFile.model_validate(data)
    except ValidationError as error:
        problems = []
        for item in error.errors(include_url=False):
            where = _field_path(tuple(item["loc"]))
            message = str(item["msg"]).removeprefix("Value error, ")
            problems.append(f"{where}: {message}" if where else message)
        raise ConnectorError(f"{origin}: " + "; ".join(problems)) from None
    return parsed


class FileConnector:
    """Catalog source backed by one export file."""

    kind = "file"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._parsed: CatalogFile | None = None

    @property
    def name(self) -> str:
        return self._load().source.name

    def _load(self) -> CatalogFile:
        if self._parsed is None:
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ConnectorError(f"{self.path}: cannot read file: {error}") from None
            self._parsed = parse_catalog(text, str(self.path))
        return self._parsed

    def check(self) -> None:
        self._load()

    def list_datasets(self) -> Iterator[CatalogDataset]:
        for dataset in self._load().datasets:
            columns = [
                c if c.ordinal is not None else c.model_copy(update={"ordinal": position})
                for position, c in enumerate(dataset.columns)
            ]
            yield dataset.model_copy(update={"columns": columns})

    def list_glossary_terms(self) -> Iterator[CatalogGlossaryTerm]:
        yield from self._load().glossary_terms
