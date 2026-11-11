"""Pydantic models of the definition files (`semantic/schema/makna.schema.json`).

The models and the JSON Schema describe the same format, and a test keeps them in
agreement. The models are strict (no coercion: a string is never read as a number),
forbid unknown keys, and reject an explicit `null` anywhere, as the schema does.

Rules that need other files (references resolve, expressions parse, join paths exist)
are not here; they belong to `makna.definitions.validate`.
"""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    TypeAdapter,
    model_validator,
)

NAME_PATTERN = r"^[a-z][a-z0-9_]{1,62}$"
QUALIFIED_REF_PATTERN = r"^[a-z][a-z0-9_]{1,62}\.[a-z][a-z0-9_]{1,62}$"
RELATED_PATTERN = r"^(domain|entity|metric|glossary_term):[a-z][a-z0-9_]{1,62}$"
TABLE_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
LANGUAGE_PATTERN = r"^[a-z]{2}$"


def _unique[T](items: list[T]) -> list[T]:
    seen: list[T] = []
    for item in items:
        if item in seen:
            raise ValueError(f"items must be unique, {item!r} appears twice")
        seen.append(item)
    return items


def _iso_date(value: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("must be a date such as 2026-07-01") from None
    return value


Name = Annotated[str, StringConstraints(pattern=NAME_PATTERN)]
QualifiedRef = Annotated[str, StringConstraints(pattern=QUALIFIED_REF_PATTERN)]
Label = Annotated[str, StringConstraints(min_length=1)]
ObjectDescription = Annotated[str, StringConstraints(min_length=20)]
PartDescription = Annotated[str, StringConstraints(min_length=10)]
IsoDate = Annotated[str, AfterValidator(_iso_date)]
Tags = Annotated[list[str], AfterValidator(_unique)]

Status = Literal["draft", "in_review", "approved", "deprecated"]
Sensitivity = Literal["public", "internal", "confidential", "personal"]
Grain = Literal["day", "week", "month", "quarter", "year"]
Scalar = str | int | float | bool


class _Model(BaseModel):
    """Common behaviour: strict types, no unknown keys, no explicit nulls."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    raise ValueError(f"{key}: null is not allowed, leave the key out")
        return data


class Owner(_Model):
    team: Annotated[str, StringConstraints(min_length=1)]
    contact: str | None = None


def _check_synonyms(value: dict[str, list[str]]) -> dict[str, list[str]]:
    for language, words in value.items():
        if not re.fullmatch(LANGUAGE_PATTERN, language):
            raise ValueError(f"synonym language {language!r} must be a two-letter ISO 639-1 code")
        if any(not word for word in words):
            raise ValueError(f"synonyms[{language}]: empty words are not allowed")
        _unique(words)
    return value


def _require_en_id(value: dict[str, list[str]]) -> dict[str, list[str]]:
    for language in ("en", "id"):
        if not value.get(language):
            raise ValueError(f"needs at least one synonym in language {language!r} (D-19)")
    return value


Synonyms = Annotated[dict[str, list[str]], AfterValidator(_check_synonyms)]
RequiredSynonyms = Annotated[
    dict[str, list[str]], AfterValidator(_check_synonyms), AfterValidator(_require_en_id)
]


class Deprecation(_Model):
    reason: Annotated[str, StringConstraints(min_length=1)]
    replaced_by: str | None = None


class _Object(_Model):
    """Fields every top-level object carries."""

    apiVersion: Literal["makna/v1"]  # noqa: N815 - the file format's key
    name: Name
    label: Label
    description: ObjectDescription
    owner: Owner
    status: Status
    sensitivity: Sensitivity
    tags: Tags | None = None
    version: Annotated[int, Field(ge=1)] | None = None
    deprecation: Deprecation | None = None


class Domain(_Object):
    kind: Literal["domain"]
    synonyms: Synonyms | None = None


RelatedRef = Annotated[str, StringConstraints(pattern=RELATED_PATTERN)]
UniqueRelated = Annotated[list[RelatedRef], AfterValidator(_unique)]


class GlossaryTerm(_Object):
    kind: Literal["glossary_term"]
    domain: Name
    synonyms: RequiredSynonyms
    related: UniqueRelated | None = None
    catalog_term: str | None = None


class MaskRedact(_Model):
    method: Literal["redact"]


class MaskTruncateDate(_Model):
    method: Literal["truncate_date"]
    grain: Literal["month", "quarter", "year"]


class MaskBucket(_Model):
    method: Literal["bucket"]
    width: Annotated[float, Field(gt=0)]


Masking = Annotated[MaskRedact | MaskTruncateDate | MaskBucket, Field(discriminator="method")]


class DimensionValue(_Model):
    value: Scalar
    description: str | None = None


class Dimension(_Model):
    name: Name
    label: str | None = None
    type: Literal["categorical", "time", "boolean", "numeric"]
    expr: Annotated[str, StringConstraints(min_length=1)] | None = None
    description: PartDescription
    synonyms: Synonyms | None = None
    sensitivity: Sensitivity | None = None
    masking: Masking | None = None
    values: list[DimensionValue] | None = None


class Measure(_Model):
    name: Name
    agg: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    expr: Annotated[str, StringConstraints(min_length=1)] | None = None
    filter: Annotated[str, StringConstraints(min_length=1)] | None = None
    time_dimension: Name
    semi_additive: Literal["last"] | None = None
    description: PartDescription
    rule: Name | None = None

    @model_validator(mode="after")
    def _expr_unless_count(self) -> Self:
        if self.agg != "count" and self.expr is None:
            raise ValueError(f"expr is required unless agg is count (agg is {self.agg})")
        return self


class KeyPair(_Model):
    from_: str = Field(alias="from")
    to: str


class Relationship(_Model):
    name: Name
    to: Name
    type: Literal["many_to_one", "one_to_one"] = "many_to_one"
    keys: Annotated[list[KeyPair], Field(min_length=1)]
    description: PartDescription


class Rule(_Model):
    name: Name
    label: str | None = None
    description: PartDescription
    expr: Annotated[str, StringConstraints(min_length=1)]
    synonyms: Synonyms | None = None


class Source(_Model):
    table: Annotated[str, StringConstraints(pattern=TABLE_PATTERN)]


class Entity(_Object):
    kind: Literal["entity"]
    domain: Name
    synonyms: RequiredSynonyms
    source: Source
    primary_key: Annotated[list[str], Field(min_length=1), AfterValidator(_unique)]
    dimensions: list[Dimension]
    measures: list[Measure] | None = None
    relationships: list[Relationship] | None = None
    rules: list[Rule] | None = None


class Filter(_Model):
    """A structured filter on a dimension. Values are data, never SQL."""

    dimension: QualifiedRef
    op: Literal["=", "!=", ">", ">=", "<", "<=", "in", "not_in", "is_null", "is_not_null"]
    value: Scalar | Annotated[list[Scalar], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _value_fits_op(self) -> Self:
        has_value = "value" in self.model_fields_set
        if self.op in ("in", "not_in"):
            if not isinstance(self.value, list):
                raise ValueError(f"op {self.op} needs value to be a list")
        elif self.op in ("is_null", "is_not_null"):
            if has_value:
                raise ValueError(f"op {self.op} takes no value")
        elif self.value is None or isinstance(self.value, list):
            raise ValueError(f"op {self.op} needs value to be a single string, number, or boolean")
        return self


class RuleRef(_Model):
    rule: QualifiedRef


def _filter_tag(value: Any) -> str:
    if isinstance(value, dict):
        return "rule" if "rule" in value else "filter"
    return "rule" if isinstance(value, RuleRef) else "filter"


MetricFilter = Annotated[
    Annotated[Filter, Tag("filter")] | Annotated[RuleRef, Tag("rule")],
    Discriminator(_filter_tag),
]


class MetricFormat(_Model):
    kind: Literal["currency", "number", "count", "percent"]
    unit: str | None = None
    decimals: Annotated[int, Field(ge=0, le=6)] | None = None


class RequestBody(_Model):
    """A structured request without `metric` (ARCHITECTURE section 7.1)."""

    dimensions: list[QualifiedRef] | None = None
    filters: list[Filter] | None = None
    grain: Grain | None = None
    time_start: IsoDate | None = None
    time_end: IsoDate | None = None
    limit: Annotated[int, Field(ge=1, le=10000)] | None = None


class ExampleRequest(_Model):
    question: Annotated[str, StringConstraints(min_length=5)]
    request: RequestBody


class Metric(_Object):
    kind: Literal["metric"]
    domain: Name
    synonyms: RequiredSynonyms
    type: Literal["simple", "ratio", "derived"]
    measure: QualifiedRef | None = None
    filters: list[MetricFilter] | None = None
    join_paths: dict[str, Annotated[list[QualifiedRef], Field(min_length=1)]] | None = None
    numerator: Name | None = None
    denominator: Name | None = None
    expr: Annotated[str, StringConstraints(min_length=1)] | None = None
    inputs: Annotated[list[Name], Field(min_length=1), AfterValidator(_unique)] | None = None
    format: MetricFormat
    time_grains: Annotated[list[Grain], Field(min_length=1), AfterValidator(_unique)] | None = None
    default_grain: Grain | None = None
    example_questions: list[Annotated[str, StringConstraints(min_length=5)]] | None = None
    caveats: str | None = None
    drafted_by: str | None = None
    example_requests: Annotated[list[ExampleRequest], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _shape_follows_type(self) -> Self:
        required: dict[str, tuple[str, ...]] = {
            "simple": ("measure",),
            "ratio": ("numerator", "denominator"),
            "derived": ("expr", "inputs"),
        }
        allowed: dict[str, set[str]] = {
            "simple": {"measure", "filters", "join_paths"},
            "ratio": {"numerator", "denominator"},
            "derived": {"expr", "inputs"},
        }
        every = {"measure", "filters", "join_paths", "numerator", "denominator", "expr", "inputs"}
        for field in required[self.type]:
            if field not in self.model_fields_set:
                raise ValueError(f"a {self.type} metric needs {field}")
        for field in sorted(every - allowed[self.type]):
            if field in self.model_fields_set:
                raise ValueError(f"a {self.type} metric must not have {field}")
        if self.join_paths is not None:
            for key in self.join_paths:
                if not re.fullmatch(NAME_PATTERN, key):
                    raise ValueError(f"join_paths key {key!r} is not a valid entity name")
        if self.status == "deprecated" and self.deprecation is None:
            raise ValueError("a deprecated metric needs deprecation")
        return self


AnyDefinition = Domain | Entity | Metric | GlossaryTerm

_ADAPTER: TypeAdapter[AnyDefinition] = TypeAdapter(
    Annotated[AnyDefinition, Field(discriminator="kind")]
)


def parse_definition(data: object) -> AnyDefinition:
    """Validate one decoded file and return its model. Raises `pydantic.ValidationError`."""
    return _ADAPTER.validate_python(data)


def dump_definition(definition: AnyDefinition) -> dict[str, Any]:
    """The JSON-compatible form of a definition, as the file spells it (only keys that were set)."""
    return definition.model_dump(mode="json", by_alias=True, exclude_unset=True)
