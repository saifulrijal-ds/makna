"""What the services return (ARCHITECTURE section 12): typed results, not formatted JSON.

Each result is a frozen model with a `to_dict()` that follows the design rules of section 12:
short keys that are full words, no nulls and no empty lists or mappings, and descriptions cut to
200 characters in lists. WP-13 serialises `to_dict()` directly. `False` and `0` are values and
are kept.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

DESCRIPTION_LIMIT = 200


def cut(text: str | None, limit: int = DESCRIPTION_LIMIT) -> str:
    """`text` on one line, at most `limit` characters; a cut text ends with an ellipsis."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def compact(value: Any) -> Any:
    """Drop None, empty strings, empty lists and empty mappings, at every depth."""
    if isinstance(value, dict):
        kept = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if not _empty(v)}
    if isinstance(value, list):
        return [item for item in (compact(v) for v in value) if not _empty(item)]
    return value


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


class View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_dict(self) -> dict[str, Any]:
        result = compact(self.model_dump(mode="json"))
        assert isinstance(result, dict)
        return result


# --- search -----------------------------------------------------------------------------------


class SearchHit(View):
    id: str
    label: str
    kind: str
    description: str
    matched_terms: list[str] = []
    type: str | None = None
    unit: str | None = None
    status: str | None = None
    """Only when the hit is not approved (a curator sees drafts and proposals)."""


class SearchOutcome(View):
    confidence: str
    guidance: str | None = None
    hits: list[SearchHit] = []

    def to_dict(self) -> dict[str, Any]:
        """Like every view, but `hits` stays when it is empty: an empty list is an answer."""
        result = super().to_dict()
        result["hits"] = [hit.to_dict() for hit in self.hits]
        return result


# --- get_definition ---------------------------------------------------------------------------


class TimeView(View):
    dimension: str | None = None
    dimensions: list[str] = []
    grains: list[str] = []
    default_grain: str | None = None
    note: str | None = None


class DimensionView(View):
    ref: str
    label: str | None = None
    description: str | None = None
    type: str
    values: list[Any] = []
    value_notes: dict[str, str] = {}
    via: str | None = None
    masked: str | None = None
    filterable: bool | None = None
    """Only `False` is shown: a dimension can be filtered on unless this says it cannot."""
    sensitivity: str | None = None
    """Shown for a personal dimension only."""
    note: str | None = None


class UnavailableDimension(View):
    ref: str
    reason: str


class RuleView(View):
    ref: str
    label: str | None = None
    description: str


class ExampleRequestView(View):
    question: str
    request: dict[str, Any]


class RelationshipView(View):
    name: str
    to: str
    description: str


class RelatedRef(View):
    id: str
    label: str
    kind: str


class IssueView(View):
    code: str
    message: str


class DefinitionView(View):
    id: str
    kind: str
    label: str
    status: str | None = None
    version: int | None = None
    description: str
    domain: str | None = None
    owner: str | None = None
    sensitivity: str | None = None
    synonyms: list[str] = []
    # metric
    formula: str | None = None
    type: str | None = None
    inputs: list[str] = []
    unit: str | None = None
    time: TimeView | None = None
    dimensions: list[DimensionView] = []
    unavailable_dimensions: list[UnavailableDimension] = []
    rules: list[RuleView] = []
    example_requests: list[ExampleRequestView] = []
    caveats: str | None = None
    example_questions: list[str] = []
    # entity
    relationships: list[RelationshipView] = []
    # glossary term
    related: list[RelatedRef] = []
    # dimension, rule, column
    values: list[Any] = []
    value_notes: dict[str, str] = {}
    entity: str | None = None
    table: str | None = None
    data_type: str | None = None
    # all kinds
    deprecation: dict[str, str] = {}
    issues: list[IssueView] = []
    warnings: list[str] = []
    """For curators and above: what to fix (entities left out of the dimensions)."""
    proposal_status: str | None = None
    """For curators and above: the object also has a proposal in this state."""


# --- list_related -----------------------------------------------------------------------------


class RelatedItem(View):
    id: str
    label: str
    kind: str
    relation: str
    description: str


class RelatedOutcome(View):
    related: list[RelatedItem] = []

    def to_dict(self) -> dict[str, Any]:
        """`related` stays when it is empty: an empty list means nothing is related."""
        return {"related": [item.to_dict() for item in self.related]}
