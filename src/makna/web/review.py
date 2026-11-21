"""The review queue and the audit page (WEB-3, GOV-1, ARCHITECTURE section 13): `/review`,
`/review/<kind>/<name>` with Approve and Reject, and `/audit`.

Approve decides what becomes official, so this module is deliberately thin:

- **Who.** `/review` is for a caller who may see drafts (`permissions.can_see_drafts`: curator,
  approver, admin), `/audit` for one who may see the audit log (`permissions.can_see_audit_log`).
  Everyone else gets the 403 page on every route, GET and POST, before a name in the path is
  looked at. The service checks the same permission again.
- **Who may decide is the service's answer.** The buttons are shown from `ReviewItem` (the
  non-raising result of `approval.may_decide` and the gate approval runs), and they are only a
  hint: `Lifecycle.approve` and `Lifecycle.reject` decide again under the lock, with the role and
  teams of the user row. This module re-implements no rule.
- **What was shown is what is decided.** Every decision carries `expected`, the commit of the
  proposal the page showed; the lifecycle refuses with `CONFLICT` when the proposal changed, and
  the page then shows the new version, so the reviewer reads it before deciding again.
- **Reads** are `Lifecycle.review_queue`, `review_item` and `audit_events`, which read without the
  lock and write nothing. A GET changes nothing.
- **Off the event loop.** Every service call, and every difference built here, runs in a worker
  thread (`run_in_thread`).
- **Escaping.** Autoescape is on, nothing is marked safe, and every text that came from a
  definition, a reason, a commit or an actor goes through the filter `visible`, which makes format
  and control characters visible (`<U+202E>`).
- **Logging.** One info line per decision with the user id, the object id and the outcome. Never
  the YAML, a reason, a commit's text, or an email.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from makna.services import LifecycleErrorCode, ServiceError, ServiceErrorCode, permissions
from makna.services.difference import MAX_DIFF_LINES, DiffRow, build_diff
from makna.services.editing import KINDS, is_object_id
from makna.services.lifecycle import MAX_REASON_LENGTH, REJECT, Lifecycle
from makna.services.reviewing import (
    DEFAULT_AUDIT_PAGE,
    MAX_AUDIT_PAGE,
    ReviewItem,
)
from makna.web.common import Problem, problem_of, run_in_thread, status_of
from makna.web.editor import KIND_LABELS, VIEW_PREFIX, edit_path
from makna.web.pages import WebContext
from makna.web.routes import _signed_in, _state

logger = logging.getLogger(__name__)

review_router = APIRouter()

NOTICES = {
    "approved": "The proposal was approved. It is now the official version.",
    "rejected": "The proposal was rejected. It is a draft again.",
}
SHOWN_ACTIONS = (
    "create_proposal",
    "update_proposal",
    "submit",
    "approve",
    "reject",
    "withdraw",
    "deprecate",
    "external_change",
    "seed",
)
"""The actions the audit page offers as a filter (the ones `audit_event` holds)."""
_COMMIT = re.compile(r"[0-9a-f]{40}")
BAD_FORM = "The form did not carry the commit of the proposal it showed. Reload the page."
TOO_LONG_TO_COMPARE = (
    f"The proposal or the official version is longer than {MAX_DIFF_LINES} lines, so the "
    "difference is not shown here and Approve is not offered: a reviewer must see what is "
    "approved. Reject it, or shorten it in the editor."
)


# --- helpers ---------------------------------------------------------------------------------


def _lifecycle(request: Request) -> Lifecycle:
    return _state(request).services.lifecycle


def _guard_review(request: Request) -> WebContext:
    """The signed-in context of a caller who may see drafts; everyone else is a 403."""
    context = _signed_in(request)
    if not permissions.can_see_drafts(context.caller):
        raise HTTPException(status_code=403)
    return context


def _guard_audit(request: Request) -> WebContext:
    context = _signed_in(request)
    if not permissions.can_see_audit_log(context.caller):
        raise HTTPException(status_code=403)
    return context


def _target(kind: str, name: str) -> str:
    """The object id of a path, or a 404 (nothing of the path is echoed)."""
    if kind not in KINDS or not is_object_id(kind, name):
        raise HTTPException(status_code=404)
    return f"{kind}:{name}"


def _item_path(kind: str, name: str) -> str:
    return f"/review/{kind}/{quote(name, safe='')}"


def _object_page(kind: str, name: str) -> str | None:
    """The discovery page of an object, or None for an id that is not one."""
    if kind not in VIEW_PREFIX or not is_object_id(kind, name):
        return None
    return f"{VIEW_PREFIX[kind]}/{quote(name, safe='')}"


def _log(action: str, context: WebContext, object_id: str | None, outcome: str) -> None:
    logger.info(
        "web: review %s: user=%s object=%s outcome=%s",
        action,
        context.caller.user_id,
        object_id or "-",
        outcome,
    )


async def _compare(item: ReviewItem) -> tuple[list[DiffRow] | None, bool]:
    """The rows of the difference, off the event loop, and whether it is too long to compare. A
    new object is compared with an empty text, so the reviewer reads all of it."""
    if item.proposal_text is None:
        return None, False
    rows = await run_in_thread(
        "review", "diff", build_diff, item.official_text or "", item.proposal_text
    )
    return rows, rows is None


def _problem_page(
    request: Request, problem: Problem, status: int, *, back: str = "/review"
) -> Response:
    return _state(request).pages.render(
        request.scope,
        "review_item.html",
        {"problem": problem, "item": None, "back": back},
        status=status,
    )


async def _render_item(
    request: Request,
    item: ReviewItem,
    *,
    problem: Problem | None = None,
    reason: str = "",
    status: int = 200,
    notice: str | None = None,
) -> Response:
    rows, too_long = await _compare(item)
    entry = item.entry
    caller = _signed_in(request).caller
    can_approve = (
        item.entry.decision.allowed
        and item.approve_refusal is None
        and not entry.unreadable
        and not too_long
    )
    return _state(request).pages.render(
        request.scope,
        "review_item.html",
        {
            "item": item,
            "entry": entry,
            "problem": problem,
            "notice": notice,
            "reason": reason[:MAX_REASON_LENGTH],
            "max_reason": MAX_REASON_LENGTH,
            "rows": rows,
            "too_long": too_long,
            "too_long_text": TOO_LONG_TO_COMPARE,
            "can_approve": can_approve,
            "can_reject": item.reject_decision.allowed and not entry.unreadable,
            "base": _item_path(entry.kind, entry.name),
            "kind_label": KIND_LABELS[entry.kind],
            "object_page": _object_page(entry.kind, entry.name),
            "edit_href": edit_path(caller, entry.kind, entry.name),
            "back": "/review",
        },
        status=status,
    )


# --- the queue -------------------------------------------------------------------------------


@review_router.get("/review")
async def queue(request: Request, notice: str = "", object: str = "") -> Response:  # noqa: A002
    context = _guard_review(request)
    pages = _state(request).pages
    try:
        found = await run_in_thread(
            "review", "queue", _lifecycle(request).review_queue, context.caller
        )
    except ServiceError as error:
        problem = problem_of(error)
        return pages.render(
            request.scope,
            "review.html",
            {"problem": problem, "read_only": bool(error.details.get("read_only"))},
            status=status_of(error),
        )
    kind, _, name = object.partition(":")
    rows = [
        {
            "e": e,
            "href": _item_path(e.kind, e.name),
            "page": _object_page(e.kind, e.name),
            "kind_label": KIND_LABELS[e.kind],
        }
        for e in found.entries
    ]
    return pages.render(
        request.scope,
        "review.html",
        {
            "queue": found,
            "rows": rows,
            "problem": None,
            "read_only": False,
            "notice": NOTICES.get(notice),
            "decided_page": _object_page(kind, name) if notice == "approved" else None,
            "decided_kind": KIND_LABELS.get(kind, ""),
            "decided_name": name if _object_page(kind, name) else "",
        },
    )


@review_router.get("/review/{kind}/{name}")
async def item_page(request: Request, kind: str, name: str) -> Response:
    context = _guard_review(request)
    object_id = _target(kind, name)
    try:
        item = await run_in_thread(
            "review", "item", _lifecycle(request).review_item, context.caller, object_id
        )
    except ServiceError as error:
        return _problem_page(request, problem_of(error), status_of(error))
    if item is None:
        raise HTTPException(status_code=404)
    return await _render_item(request, item)


# --- the decisions ---------------------------------------------------------------------------


async def _decide(
    request: Request, kind: str, name: str, action: str, reason: str, expected: str
) -> Response:
    context = _guard_review(request)
    object_id = _target(kind, name)
    life = _lifecycle(request)
    caller = context.caller
    try:
        if not _COMMIT.fullmatch(expected):
            raise ServiceError(ServiceErrorCode.INVALID_INPUT, BAD_FORM, {"field": "expected"})
        decide = life.reject if action == REJECT else life.approve
        await run_in_thread("review", action, decide, caller, object_id, reason, expected)
    except ServiceError as error:
        return await _refused(request, context, action, kind, name, object_id, reason, error)
    _log(action, context, object_id, "ok")
    notice = "rejected" if action == REJECT else "approved"
    query = urlencode({"notice": notice, "object": object_id})
    return RedirectResponse(f"/review?{query}", status_code=303)


async def _refused(
    request: Request,
    context: WebContext,
    action: str,
    kind: str,
    name: str,
    object_id: str,
    reason: str,
    error: ServiceError,
) -> Response:
    """The page after a decision was refused: the problem, and the proposal as it is now, with the
    reason as it was typed. A proposal that changed is shown in its new state, so the reviewer
    reads it (and the form carries its new commit) before deciding again."""
    _log(action, context, object_id, str(error.code))
    if error.code == ServiceErrorCode.NOT_FOUND:
        raise HTTPException(status_code=404)
    problem = problem_of(error)
    try:
        item = await run_in_thread(
            "review", "item", _lifecycle(request).review_item, context.caller, object_id
        )
    except ServiceError:
        item = None
    if item is None:
        return _problem_page(request, problem, status_of(error))
    return await _render_item(
        request, item, problem=problem, reason=reason, status=status_of(error)
    )


@review_router.post("/review/{kind}/{name}/approve")
async def approve(
    request: Request,
    kind: str,
    name: str,
    reason: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
) -> Response:
    return await _decide(request, kind, name, "approve", reason, expected)


@review_router.post("/review/{kind}/{name}/reject")
async def reject(
    request: Request,
    kind: str,
    name: str,
    reason: Annotated[str, Form()] = "",
    expected: Annotated[str, Form()] = "",
) -> Response:
    return await _decide(request, kind, name, "reject", reason, expected)


# --- the audit page --------------------------------------------------------------------------


def _audit_link(object_id: str, action: str, before: int | None) -> str:
    fields = {k: v for k, v in (("object", object_id), ("action", action)) if v}
    if before is not None:
        fields["before"] = str(before)
    return "/audit" + (f"?{urlencode(fields)}" if fields else "")


@review_router.get("/audit")
async def audit(
    request: Request,
    object: str = "",  # noqa: A002
    action: str = "",
    before: str = "",
    size: str = "",
) -> Response:
    context = _guard_audit(request)
    pages = _state(request).pages
    wanted = object.strip()
    chosen = action.strip()
    problem: Problem | None = None
    page = None
    cursor: int | None = None
    if before.strip():
        try:
            cursor = int(before.strip())
        except ValueError:
            problem = _bad_filter("before")
    limit = DEFAULT_AUDIT_PAGE
    if size.strip():
        try:
            limit = max(1, min(int(size.strip()), MAX_AUDIT_PAGE))
        except ValueError:
            problem = _bad_filter("size")
    status = 400 if problem is not None else 200
    if problem is None:
        try:
            page = await run_in_thread(
                "audit",
                "events",
                _lifecycle(request).audit_events,
                context.caller,
                object_id=wanted or None,
                action=chosen or None,
                before=cursor,
                limit=limit,
            )
        except ServiceError as error:
            problem = Problem(
                str(error.code),
                "That filter cannot be used."
                if error.code == ServiceErrorCode.INVALID_INPUT
                else "The audit log cannot be shown.",
                _filter_message(error),
            )
            status = status_of(error)
    rows = []
    for row in page.rows if page is not None else ():
        kind, _, name = row.object_id.partition(":")
        rows.append(
            {
                "r": row,
                "object_page": _object_page(kind, name),
                "external": row.actor_label.startswith("external:"),
                "when": row.at.strftime("%Y-%m-%d %H:%M:%S"),
                "filter": _audit_link(row.object_id, "", None)
                if is_object_id(kind, name)
                else None,
            }
        )
    keep = {"object": wanted, "action": chosen}
    return pages.render(
        request.scope,
        "audit.html",
        {
            "rows": rows,
            "problem": problem,
            "object_filter": wanted if problem is None else "",
            "action_filter": chosen if chosen in SHOWN_ACTIONS else "",
            "actions": SHOWN_ACTIONS,
            "older": _audit_link(
                keep["object"] if problem is None else "",
                keep["action"] if chosen in SHOWN_ACTIONS else "",
                page.older if page is not None else None,
            )
            if page is not None and page.older is not None
            else None,
            "all_link": "/audit",
            "page_size": limit,
        },
        status=status,
    )


def _bad_filter(field: str) -> Problem:
    return Problem(
        str(ServiceErrorCode.INVALID_INPUT),
        "That filter cannot be used.",
        f"{field}: a whole number is needed.",
    )


def _filter_message(error: ServiceError) -> str:
    """The service's message names the field and what is allowed, never the value sent."""
    if error.code in (ServiceErrorCode.INVALID_INPUT, LifecycleErrorCode.CONFLICT):
        return error.message
    return "Try again in a little while."
