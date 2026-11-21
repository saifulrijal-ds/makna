"""What the editor (WP-19) and the review pages (WP-19B) share: the headline and the HTTP status
of every error code a lifecycle call can refuse with, the `Problem` a page shows, and the helper
that runs a service call in a worker thread."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
from fastapi import HTTPException

from makna.services import LifecycleErrorCode, ServiceError, ServiceErrorCode
from makna.services.editing import CheckIssue
from makna.services.lifecycle import COMMITTED_NOT_INDEXED, MAX_MESSAGE_SHOWN

logger = logging.getLogger(__name__)

HEADLINES: dict[str, str] = {
    str(ServiceErrorCode.NOT_FOUND): "There is no such object.",
    str(ServiceErrorCode.PERMISSION_DENIED): "You are not allowed to do this.",
    str(ServiceErrorCode.INVALID_INPUT): "Something in the form cannot be used.",
    str(ServiceErrorCode.CATALOG_NOT_SYNCED): "The catalog has not been synced yet.",
    str(ServiceErrorCode.INTERNAL): "Makna could not complete this.",
    str(LifecycleErrorCode.DEFINITION_INVALID): "The definition has errors.",
    str(LifecycleErrorCode.OPEN_REFERENCE_ISSUE): "The definition has a reference issue.",
    str(LifecycleErrorCode.CONFLICT): "The change cannot be made now.",
    str(LifecycleErrorCode.TOO_MANY_DRAFTS): "You have too many open proposals.",
}
"""One short sentence for every code a lifecycle call can refuse with (a test checks that none is
missing). The service's own message follows it on the page."""
STATUS_OF: dict[str, int] = {
    str(ServiceErrorCode.NOT_FOUND): 404,
    str(ServiceErrorCode.PERMISSION_DENIED): 403,
    str(ServiceErrorCode.INVALID_INPUT): 400,
    str(ServiceErrorCode.CATALOG_NOT_SYNCED): 409,
    str(ServiceErrorCode.INTERNAL): 500,
    str(LifecycleErrorCode.DEFINITION_INVALID): 422,
    str(LifecycleErrorCode.OPEN_REFERENCE_ISSUE): 409,
    str(LifecycleErrorCode.CONFLICT): 409,
    str(LifecycleErrorCode.TOO_MANY_DRAFTS): 409,
}
COMMITTED_HEADLINE = "The change was made official, but the index has not caught up yet."
"""The headline for `COMMITTED_NOT_INDEXED`: the commit and its audit record exist, so the change
is official; only the index is behind, and `makna index sync` finishes it."""


@dataclass(frozen=True)
class Problem:
    """A refusal to show: the code, a short headline, the service's message, and the problems of
    the definition when there are some."""

    code: str
    headline: str
    message: str
    issues: tuple[CheckIssue, ...] = ()
    link: str | None = None
    link_text: str = ""


def problem_of(error: ServiceError, *, link: str | None = None, link_text: str = "") -> Problem:
    """The `Problem` of a refusal. The message is the service's own, fixed text; the issues are
    those of a `DEFINITION_INVALID` refusal."""
    code = str(error.code)
    raw = (
        error.details.get("issues") if code == str(LifecycleErrorCode.DEFINITION_INVALID) else None
    )
    issues: list[CheckIssue] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                line = item.get("line")
                issues.append(
                    CheckIssue(
                        str(item.get("rule", "")),
                        line if isinstance(line, int) else None,
                        str(item.get("file", "")),
                        str(item.get("message", ""))[:MAX_MESSAGE_SHOWN],
                        True,
                    )
                )
    headline = HEADLINES.get(code, "The change cannot be made.")
    if error.message == COMMITTED_NOT_INDEXED:
        headline = COMMITTED_HEADLINE
    return Problem(code, headline, error.message, tuple(issues), link, link_text)


def status_of(error: ServiceError) -> int:
    return STATUS_OF.get(str(error.code), 400)


async def run_in_thread[T](area: str, name: str, function: Callable[..., T], *args: Any) -> T:
    """Run a service call in a worker thread. A `ServiceError` passes through; anything else (a
    database error, a repository error, a recursion error) becomes the fixed 500 page, and only
    the class of the exception is logged, never its text (it can hold a value of the request)."""
    try:
        return await anyio.to_thread.run_sync(functools.partial(function, *args))
    except ServiceError:
        raise
    except Exception as error:
        logger.error("web: %s %s failed: error=%s", area, name, type(error).__name__)
        raise HTTPException(status_code=500) from None
