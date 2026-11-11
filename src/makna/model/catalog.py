"""The common catalog model (ARCHITECTURE section 6.1, CON-1).

Every catalog connector returns these models. They are the smallest set that a catalog such
as OpenMetadata or DataHub, and a hand-written export, can all fill.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

ENGINE_FQN_PATTERN = r"^[^.\s]+\.[^.\s]+\.[^.\s]+$"

DataType = Literal["string", "integer", "decimal", "float", "boolean", "date", "timestamp", "other"]
OwnerType = Literal["user", "team"]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _unique(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("items must be unique")
    return values


class CatalogOwner(_Model):
    name: NonEmpty
    type: OwnerType
    email: str | None = None


class CatalogColumn(_Model):
    name: NonEmpty
    data_type: DataType
    native_type: str | None = None
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    nullable: bool = True
    is_primary_key: bool = False
    ordinal: Annotated[int, Field(ge=0)] | None = None
    """Position in the table, from 0. A file may leave it out: its list position is used."""

    _unique_tags = field_validator("tags")(_unique)


class CatalogDataset(_Model):
    engine_fqn: Annotated[str, StringConstraints(pattern=ENGINE_FQN_PATTERN)]
    catalog_id: str | None = None
    name: NonEmpty
    description: str = ""
    owners: list[CatalogOwner] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    columns: list[CatalogColumn] = Field(default_factory=list)
    updated_at: Annotated[datetime | None, Field(strict=False)] = None

    _unique_tags = field_validator("tags")(_unique)

    @field_validator("columns")
    @classmethod
    def _unique_column_names(cls, columns: list[CatalogColumn]) -> list[CatalogColumn]:
        names = [c.name for c in columns]
        for name in names:
            if names.count(name) > 1:
                raise ValueError(f"column name {name!r} appears twice")
        return columns


class CatalogGlossaryTerm(_Model):
    catalog_id: NonEmpty
    name: NonEmpty
    definition: str = ""
    synonyms: list[str] = Field(default_factory=list)
    related_terms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
