"""Errors of the service layer (ARCHITECTURE section 12, "Service errors settled in WP-12").

`ServiceError` has the shape of `makna.model.request.QueryError`: a code, a message that says
what to do, and details an agent can act on. Details hold names and the caller's own references,
never data values. A `QueryError` from the compiler passes through the services with its own
code; these are the codes of the services themselves.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ServiceErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_INPUT = "INVALID_INPUT"
    CATALOG_NOT_SYNCED = "CATALOG_NOT_SYNCED"
    INTERNAL = "INTERNAL"


class LifecycleErrorCode(StrEnum):
    """The codes only the write path (WP-18) uses. They are separate from `ServiceErrorCode`,
    which is the set of the four read tools, so that no read tool can return them."""

    DEFINITION_INVALID = "DEFINITION_INVALID"
    """The definition has errors; `details.issues` lists rule code, line, and message."""
    OPEN_REFERENCE_ISSUE = "OPEN_REFERENCE_ISSUE"
    """An approval is refused because the object has an open reference issue."""
    TOO_MANY_DRAFTS = "TOO_MANY_DRAFTS"
    """The caller already has the most open proposals one user may have (WP-19, RS-I1)."""
    CONFLICT = "CONFLICT"
    """The change cannot be made now: another one is in progress, or the object is in another
    state than the action needs."""


class ServiceError(Exception):
    """A service call that cannot be answered: a code, a message, and details."""

    def __init__(
        self,
        code: ServiceErrorCode | LifecycleErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details if details is not None else {}

    def to_dict(self) -> dict[str, Any]:
        """The body of a tool error (section 12): `{"error": {code, message, details}}`."""
        return {"error": {"code": str(self.code), "message": self.message, "details": self.details}}


class PermissionDenied(ServiceError, PermissionError):
    """The caller's role does not allow this (`PERMISSION_DENIED`). It is also a built-in
    `PermissionError`, so either name catches it."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ServiceErrorCode.PERMISSION_DENIED, message, details)


def invalid_input(field: str, problem: str, **details: Any) -> ServiceError:
    """`INVALID_INPUT` naming the field. `problem` says what is allowed, not what was sent."""
    return ServiceError(
        ServiceErrorCode.INVALID_INPUT,
        f"{field}: {problem}",
        {"field": field, **details},
    )
