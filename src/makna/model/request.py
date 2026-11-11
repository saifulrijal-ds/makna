"""The compile request, its response, and the structured errors (ARCHITECTURE 7.1, 7.2, 7.7).

The request is deliberately lenient about what planning must judge itself: a bad grain, an
operator that does not fit, a date that is not a date, or a limit that is too large reaches the
planner and comes back as an error with a code and details, never as a validation exception.
Only the shape (field names and basic types) is checked here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from makna.model.definitions import RequestBody, Scalar

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000
FILTER_OPS = ("=", "!=", ">", ">=", "<", "<=", "in", "not_in", "is_null", "is_not_null")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RequestFilter(_Frozen):
    """A filter as sent by a caller: `op` and `value` are judged by the planner."""

    dimension: str
    op: str
    value: Scalar | tuple[Scalar, ...] | None = None


class QueryRequest(_Frozen):
    """One metric, with the dimensions, filters, grain, and time range wanted (section 7.1)."""

    metric: str
    dimensions: tuple[str, ...] = ()
    filters: tuple[RequestFilter, ...] = ()
    grain: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    limit: int = DEFAULT_LIMIT

    @classmethod
    def from_example(cls, metric: str, body: RequestBody) -> QueryRequest:
        """The request an `example_requests` entry stands for (D-18)."""
        return cls(
            metric=metric,
            dimensions=tuple(body.dimensions or ()),
            filters=tuple(
                RequestFilter(
                    dimension=f.dimension,
                    op=f.op,
                    value=tuple(f.value) if isinstance(f.value, list) else f.value,
                )
                for f in body.filters or ()
            ),
            grain=body.grain,
            time_start=body.time_start,
            time_end=body.time_end,
            limit=body.limit if body.limit is not None else DEFAULT_LIMIT,
        )


class ErrorCode(StrEnum):
    """The error codes of section 7.7."""

    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    NOT_APPROVED = "NOT_APPROVED"
    DEPRECATED = "DEPRECATED"
    BROKEN_REFERENCE = "BROKEN_REFERENCE"
    UNKNOWN_DIMENSION = "UNKNOWN_DIMENSION"
    DIMENSION_NOT_AVAILABLE = "DIMENSION_NOT_AVAILABLE"
    NO_JOIN_PATH = "NO_JOIN_PATH"
    AMBIGUOUS_JOIN_PATH = "AMBIGUOUS_JOIN_PATH"
    INVALID_GRAIN = "INVALID_GRAIN"
    INVALID_FILTER = "INVALID_FILTER"
    INVALID_TIME_RANGE = "INVALID_TIME_RANGE"
    PERSONAL_DATA_REFUSED = "PERSONAL_DATA_REFUSED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"


class QueryError(Exception):
    """A request that cannot be compiled: a code, a message that says what to do, and details."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details if details is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """The body of a tool error (section 12): `{"error": {code, message, details}}`."""
        return {"error": {"code": str(self.code), "message": self.message, "details": self.details}}


class ResultColumn(_Frozen):
    """One column of the result, in the order of section 7.2."""

    name: str
    role: Literal["time", "dimension", "input", "metric"]
    unit: str | None = None


class MetricRef(_Frozen):
    id: str
    version: int | None = None


class CompileResponse(_Frozen):
    """What `makna_compile_query` returns (section 12). SQL generation (WP-09) fills it."""

    sql: str
    dialect: Literal["postgres", "duckdb"]
    columns: tuple[ResultColumn, ...]
    explanation: tuple[str, ...] = ()
    warnings: tuple[str, ...] = Field(default=())
    metric: MetricRef
