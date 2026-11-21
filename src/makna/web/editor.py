"""The definition editor (WEB-2, ARCHITECTURE section 13): `/edit/<kind>/<name>` and
`/new/<kind>`, with the actions Check, Save draft, and Submit for review.

This is the first web route that writes definitions, so it is deliberately thin:

- **Who.** Only a signed-in user who may edit proposals (`permissions.can_edit_proposals`: curator,
  approver, admin) sees or posts to any route here; anyone else gets the 403 page, before the
  name in the path is looked at. Which actions are allowed on which object (owner team, authorship,
  deprecated objects, the two-person rule) is decided by `Lifecycle` under its lock. This module
  shows the refusal and never re-implements a rule.
- **Writes.** Every write is a `Lifecycle` call (`create_proposal`, `update_proposal`, `submit`),
  which writes the canonical model as a git commit with the signed-in user as author and an
  `audit_event` row. This module writes no file and no row. A GET never changes anything.
- **Check** is `Lifecycle.check`: it writes nothing and takes no lock. It is not logged to
  `tool_call_log` (it is not a tool, and the page reads of the discovery pages are not either);
  the lifecycle actions are recorded in `audit_event`, which is their log. This module writes one
  info line for each action, with the user id, the object id and the outcome, and never the text,
  the reason, or an exception's message.
- **No lost update.** The form carries `expected`, the SHA-256 of the proposal as the editor
  loaded it (or `none` / `new`); the lifecycle compares it under its lock and answers
  `CONFLICT`. The page then shows the conflict and keeps what the user typed.
- **Escaping.** Autoescape is on and nothing here (the YAML, the reason, an issue's message, a
  difference row, a name) is marked safe. The difference view is built here from `difflib`
  opcodes and handed to the template as plain strings, never as `difflib.HtmlDiff` output.
- **Without script** the text area is the editor and each button is a form post. `editor.js`
  only attaches Ace to the text area.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from makna.services import Caller, LifecycleErrorCode, ServiceError, ServiceErrorCode, permissions
from makna.services.difference import MAX_DIFF_LINES, DiffRow, build_diff
from makna.services.editing import (
    KINDS,
    MAX_CHECK_ISSUES,
    MAX_YAML_BYTES,
    NEW_OBJECT,
    NO_PROPOSAL,
    CheckIssue,
    CheckResult,
    EditorSource,
    is_object_id,
    name_in,
)
from makna.services.lifecycle import (
    EXISTS_MESSAGE,
    MAX_MESSAGE_SHOWN,
    MAX_REASON_LENGTH,
    READ_ONLY_MESSAGE,
    ActionResult,
    Lifecycle,
    as_proposal_text,
)
from makna.web.common import (
    HEADLINES,
    Problem,
    problem_of,
    run_in_thread,
    status_of,
)
from makna.web.pages import WebContext
from makna.web.routes import _signed_in, _state

logger = logging.getLogger(__name__)

editor_router = APIRouter()

TEMPLATES = Path(__file__).parent / "definition_templates"
"""One file per kind of `KINDS`: `<kind>.yaml`. They are files of the package, not built
from the model, so a person can read and change them; `template_text` fills in the owner team."""

KIND_LABELS = {
    "metric": "metric",
    "entity": "entity",
    "glossary_term": "glossary term",
    "domain": "domain",
}
VIEW_PREFIX = {
    "metric": "/metrics",
    "entity": "/entities",
    "glossary_term": "/glossary",
    "domain": "/domains",
}
"""Where the discovery page of each kind is (the editor links to it)."""
NOTICES = {
    "saved": "The draft was saved. It stays a proposal until an approver approves it.",
    "submitted": "The proposal was submitted for review.",
    "withdrawn": "The proposal was withdrawn.",
}

# --- templates -------------------------------------------------------------------------------


def template_text(kind: str, team: str | None) -> str:
    """The template of `kind` with `owner.team` set to `team` when there is one. Every other
    field the author must fill is left empty, so Check names it."""
    if kind not in KINDS:
        raise ValueError("not a kind of definition")
    text = (TEMPLATES / f"{kind}.yaml").read_text(encoding="utf-8")
    if team:
        # A JSON string is a valid double-quoted YAML scalar, whatever the team is called.
        text = text.replace('  team: ""', f"  team: {json.dumps(team)}", 1)
    return text


def edit_path(caller: Caller, kind: str, name: str) -> str | None:
    """The editor's address for an object, or None for a caller who may not edit."""
    if not permissions.can_edit_proposals(caller) or not is_object_id(kind, name):
        return None
    return f"/edit/{kind}/{quote(name, safe='')}"


# --- the difference view ---------------------------------------------------------------------


@dataclass(frozen=True)
class Diff:
    """What the page shows under "Difference": rows, or the reason there are none."""

    rows: tuple[DiffRow, ...] = ()
    note: str = ""
    identical: bool = False


NEW_OBJECT_DIFF = Diff(
    note="This is a new object, so there is no official version to compare with."
)


def make_diff(official_text: str | None, text: str, version: int | None) -> Diff:
    """Compare the official version with `text`, both in the canonical form of a proposal
    (status `draft`, no deprecation, the version of the official object), so that what Makna sets
    itself does not show as a change. A new object has nothing to compare with."""
    if official_text is None:
        return NEW_OBJECT_DIFF
    proposed = as_proposal_text(text, version)
    if proposed is None:
        return Diff(note="The text cannot be read as a definition yet, so it is not compared.")
    rows = build_diff(official_text, proposed)
    if rows is None:
        return Diff(
            note=f"The text is longer than {MAX_DIFF_LINES} lines, so it is not compared here."
        )
    return Diff(tuple(rows), identical=not rows)


# --- what the page shows ---------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The object an editor request is about: an existing one (`/edit/<kind>/<name>`) or a new
    one (`/new/<kind>`, whose name comes from the text)."""

    kind: str
    name: str | None

    @property
    def is_new(self) -> bool:
        return self.name is None

    @property
    def object_id(self) -> str | None:
        return f"{self.kind}:{self.name}" if self.name is not None else None

    @property
    def base(self) -> str:
        if self.name is None:
            return f"/new/{self.kind}"
        return f"/edit/{self.kind}/{self.name}"


@dataclass
class Page:
    target: Target
    text: str
    reason: str = ""
    expected: str = NO_PROPOSAL
    source: EditorSource | None = None
    notice: str | None = None
    problem: Problem | None = None
    check: CheckResult | None = None
    diff: Diff | None = None
    stored: str | None = None
    """What was kept although the action did not finish (the draft that was saved before a
    submit was refused)."""
    facts: list[str] = field(default_factory=list)
    show_form: bool = True
    """False for the page of an editor that cannot open and has no text to keep."""


def _facts(target: Target, source: EditorSource | None) -> list[str]:
    """What the page says about where the text came from. Fixed sentences and numbers."""
    if source is None:
        return [
            "This is a new object. Saving stores a draft proposal; nothing is official until an "
            "approver approves it."
        ]
    status = (source.status or "unreadable").replace("_", " ")
    official = (
        "There is no official version yet."
        if source.official_text is None
        else f"The official version is number {source.version}."
    )
    if source.loaded == "proposal":
        loaded = f"Loaded: the open proposal (status {status}). {official}"
    else:
        loaded = (
            f"Loaded: the official version (number {source.version}, status {status}). Saving "
            "creates a draft proposal; the official version keeps being served until a proposal "
            "is approved."
        )
    if source.external:
        loaded += " The proposal's content was made outside Makna."
    return [loaded]


def _render(request: Request, page: Page, status: int = 200) -> Response:
    state = _state(request)
    target = page.target
    if page.diff is None and target.is_new:
        page.diff = NEW_OBJECT_DIFF
    other_kinds = [(k, KIND_LABELS[k]) for k in KINDS if k != target.kind]
    return state.pages.render(
        request.scope,
        "editor.html",
        {
            "target": target,
            "kind_label": KIND_LABELS[target.kind],
            "base": target.base,
            "text": page.text,
            "reason": page.reason[:MAX_REASON_LENGTH],
            "expected": page.expected,
            "max_reason": MAX_REASON_LENGTH,
            "notice": page.notice,
            "problem": page.problem,
            "check": page.check,
            "diff": page.diff,
            "stored": page.stored,
            "facts": page.facts or _facts(target, page.source),
            "blocked": page.source.blocked if page.source is not None else None,
            "show_form": page.show_form,
            "can_withdraw": (
                page.source.can_withdraw if page.source is not None and not target.is_new else False
            ),
            "other_kinds": other_kinds,
            "object_link": (
                f"{VIEW_PREFIX[target.kind]}/{target.name}" if target.name is not None else None
            ),
        },
        status=status,
    )


# --- the work, off the event loop ------------------------------------------------------------


def _size(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


def _newlines(text: str) -> str:
    """Browsers send a text area's line breaks as CRLF; the text is kept with LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


async def _run[T](name: str, function: Callable[..., T], *args: Any) -> T:
    """`run_in_thread` for the editor: a worker thread, and the fixed 500 page for anything that
    is not a `ServiceError`."""
    return await run_in_thread("editor", name, function, *args)


def _guard(request: Request) -> WebContext:
    """The signed-in context of a caller who may edit proposals; everyone else is a 403."""
    context = _signed_in(request)
    if not permissions.can_edit_proposals(context.caller):
        raise HTTPException(status_code=403)
    return context


def _target(kind: str, name: str | None = None) -> Target:
    if kind not in KINDS:
        raise HTTPException(status_code=404)
    if name is not None and not is_object_id(kind, name):
        raise HTTPException(status_code=404)
    return Target(kind, name)


def _lifecycle(request: Request) -> Lifecycle:
    return _state(request).services.lifecycle


def _log(action: str, context: WebContext, object_id: str | None, outcome: str) -> None:
    logger.info(
        "web: editor %s: user=%s object=%s outcome=%s",
        action,
        context.caller.user_id,
        object_id or "-",
        outcome,
    )


async def _source(life: Lifecycle, caller: Caller, object_id: str | None) -> EditorSource | None:
    """What the editor knows of an object for the page around a posted text; None when there is
    nothing (a new object) or the read is refused (the refusal is shown by the action itself)."""
    if object_id is None:
        return None
    try:
        return await _run("read", life.editor_source, caller, object_id)
    except ServiceError:
        return None


def _name_in(text: str) -> tuple[str | None, CheckResult]:
    """The name a new object's text gives itself, or the V01 problems that stop it from having
    one, as a `CheckResult` (at most `MAX_CHECK_ISSUES` of them, with the count of all). The kind
    is checked by `Lifecycle.check` and by the actions. Parses the text: call it through `_run`."""
    name, problems = name_in(text)
    shown = tuple(
        CheckIssue(rule, line, "your text", message[:MAX_MESSAGE_SHOWN], True)
        for rule, line, message in problems[:MAX_CHECK_ISSUES]
    )
    return name, CheckResult(shown, len(problems), False)


async def _diff(official_text: str | None, text: str, version: int | None) -> Diff:
    """`make_diff` off the event loop: it parses and renders the text."""
    return await _run("diff", make_diff, official_text, text, version)


# --- pages -----------------------------------------------------------------------------------


@editor_router.get("/new")
async def new_index(request: Request, notice: str = "") -> Response:
    _guard(request)
    return _state(request).pages.render(
        request.scope,
        "new.html",
        {"kinds": [(k, KIND_LABELS[k]) for k in KINDS], "notice": NOTICES.get(notice)},
    )


@editor_router.get("/new/{kind}")
async def new_form(request: Request, kind: str) -> Response:
    context = _guard(request)
    target = _target(kind)
    try:
        writable = await _run("mode", _lifecycle(request).writable)
    except ServiceError as error:
        return _render(request, _unavailable(target, error), status_of(error))
    if not writable:
        return _render(request, _unavailable(target, ServiceError(
            LifecycleErrorCode.CONFLICT, READ_ONLY_MESSAGE, {"read_only": True}
        )), 409)  # fmt: skip
    teams = context.caller.teams
    page = Page(target, template_text(kind, teams[0] if teams else None), expected=NEW_OBJECT)
    return _render(request, page)


@editor_router.get("/edit/{kind}/{name}")
async def edit_form(request: Request, kind: str, name: str, notice: str = "") -> Response:
    context = _guard(request)
    target = _target(kind, name)
    life = _lifecycle(request)
    try:
        source = await _run("read", life.editor_source, context.caller, str(target.object_id))
    except ServiceError as error:
        return _render(request, _unavailable(target, error), status_of(error))
    if source is None:
        raise HTTPException(status_code=404)
    page = Page(
        target,
        source.text,
        expected=source.proposal_sha256,
        source=source,
        notice=NOTICES.get(notice),
    )
    page.diff = await _diff(source.official_text, source.text, source.version)
    return _render(request, page)


def _unavailable(target: Target, error: ServiceError, text: str = "") -> Page:
    """The page for an object whose editor cannot open (a plain directory, a broken repository,
    a refused role): the problem, and the text that was posted, if any."""
    return Page(target, text, problem=problem_of(error), facts=[], show_form=bool(text))


# --- the actions -----------------------------------------------------------------------------


async def _check(
    life: Lifecycle, caller: Caller, target: Target, text: str
) -> tuple[str | None, CheckResult]:
    """The object id a check was made for and its result. For a new object the id comes from
    the text; when the text cannot be read, the result is its V01 problems."""
    if target.object_id is not None:
        return target.object_id, await _run("check", life.check, caller, target.object_id, text)
    name, found = await _run("parse", _name_in, text)
    if name is None:
        return None, found
    object_id = f"{target.kind}:{name}"
    return object_id, await _run("check", life.check, caller, object_id, text)


async def _save(
    life: Lifecycle, caller: Caller, target: Target, object_id: str, text: str, reason: str,
    expected: str,
) -> ActionResult:  # fmt: skip
    """Save draft: `create_proposal` for an object that has no proposal, else `update_proposal`.
    A new object is created with `new`, so that a name that exists is refused, never turned into
    a change of the existing object."""
    if target.is_new:
        return await _run(
            "create", life.create_proposal, caller, object_id, text, reason, NEW_OBJECT
        )
    if expected in (NO_PROPOSAL, NEW_OBJECT):
        return await _run("create", life.create_proposal, caller, object_id, text, reason, expected)
    return await _run("update", life.update_proposal, caller, object_id, text, reason, expected)


async def _post(
    request: Request, target: Target, action: str, yaml_text: str, reason: str, expected: str
) -> Response:
    context = _signed_in(request)  # who may edit was decided by the handler that called this
    caller = context.caller
    life = _lifecycle(request)
    text = _newlines(yaml_text)
    if _size(text) > MAX_YAML_BYTES:
        # Not echoed: the page is the fixed 413 page.
        _log(action, context, target.object_id, "too_large")
        return _state(request).pages.error(request.scope, 413)
    if target.is_new:
        expected = NEW_OBJECT
    page = Page(target, text, reason=reason, expected=expected)
    object_id = target.object_id
    if object_id is not None:
        # `/edit` is for an object that exists: anything else is a 404, never a quiet "new".
        try:
            page.source = await _run("read", life.editor_source, caller, object_id)
        except ServiceError as error:
            return _render(request, _unavailable(target, error, text), status_of(error))
        if page.source is None:
            raise HTTPException(status_code=404)

    if action == "check":
        try:
            object_id, result = await _check(life, caller, target, text)
        except ServiceError as error:
            page.problem = problem_of(error)
            _log(action, context, object_id, str(error.code))
            return _render(request, page, status_of(error))
        page.check = result
        if page.source is not None:
            page.diff = await _diff(page.source.official_text, text, page.source.version)
        elif object_id is not None:
            page.diff = await _diff(None, text, None)
        if target.is_new and result.exists and object_id is not None:
            page.problem = _exists(target.kind, object_id)
        _log(action, context, object_id, "ok" if result.ok else "problems")
        return _render(request, page)

    if object_id is None:
        name, found = await _run("parse", _name_in, text)
        if name is None:  # a new object whose text cannot be read: nothing to save yet
            page.check = found
            page.problem = Problem(
                str(LifecycleErrorCode.DEFINITION_INVALID),
                "The text cannot be saved yet.",
                "Fix the problems below, then save again.",
            )
            _log(action, context, None, "unreadable")
            return _render(request, page, 422)
        object_id = f"{target.kind}:{name}"

    if action == "save":
        try:
            await _save(life, caller, target, object_id, text, reason, expected)
        except ServiceError as error:
            return await _failed(request, context, action, page, object_id, error, None)
        _log(action, context, object_id, "ok")
        return _saved(object_id, "saved")

    # Submit for review: save first when the text differs from the stored proposal (two commits,
    # both with the reason), then submit exactly the proposal that was saved.
    saved: ActionResult | None = None
    try:
        try:
            saved = await _save(life, caller, target, object_id, text, reason, expected)
        except ServiceError as error:
            if not (error.code == LifecycleErrorCode.CONFLICT and error.details.get("unchanged")):
                raise
        proposal = (
            saved.proposal_sha256 if saved is not None and saved.proposal_sha256 else expected
        )
        await _run("submit", life.submit, caller, object_id, reason, proposal)
    except ServiceError as error:
        return await _failed(request, context, action, page, object_id, error, saved)
    _log(action, context, object_id, "ok")
    return _saved(object_id, "submitted")


async def _withdraw(request: Request, target: Target, reason: str, expected: str) -> Response:
    """Withdraw proposal: `Lifecycle.withdraw`, which decides who may (the author, an approver or an
    administrator) and what is put back (an outside edit of an official file is restored). The one
    way out of a proposal the editor cannot show (over 256 KiB, or not text)."""
    context = _signed_in(request)  # who may edit was decided by the handler that called this
    caller, life = context.caller, _lifecycle(request)
    object_id = str(target.object_id)
    try:
        done = await _run("withdraw", life.withdraw, caller, object_id, reason, expected)
    except ServiceError as error:
        source = await _source(life, caller, object_id)
        if source is None and error.code == ServiceErrorCode.NOT_FOUND:
            raise HTTPException(status_code=404) from None
        page = Page(target, source.text if source is not None else "", reason=reason)
        page.source = source
        page.problem = problem_of(error)
        if source is not None:
            page.expected = source.proposal_sha256
            page.diff = await _diff(source.official_text, source.text, source.version)
        _log("withdraw", context, object_id, str(error.code))
        return _render(request, page, status_of(error))
    _log("withdraw", context, object_id, "ok")
    if done.version is not None:  # the object has an official version: back to its editor
        return _saved(object_id, "withdrawn")
    return RedirectResponse("/new?notice=withdrawn", status_code=303)


def _saved(object_id: str, notice: str) -> Response:
    """After a save or a submit the browser is sent to the editor of the object (so a reload does
    not post again), which loads what was stored."""
    kind, _, name = object_id.partition(":")
    return RedirectResponse(f"/edit/{kind}/{quote(name, safe='')}?notice={notice}", status_code=303)


def _exists(kind: str, object_id: str) -> Problem:
    name = object_id.partition(":")[2]
    return Problem(
        str(LifecycleErrorCode.CONFLICT),
        HEADLINES[str(LifecycleErrorCode.CONFLICT)],
        EXISTS_MESSAGE,
        link=f"/edit/{kind}/{quote(name, safe='')}",
        link_text="Open it in the editor",
    )


async def _failed(
    request: Request,
    context: WebContext,
    action: str,
    page: Page,
    object_id: str,
    error: ServiceError,
    saved: ActionResult | None,
) -> Response:
    """The page after an action was refused: the problem, and the text exactly as it was typed.
    If the draft had been saved before the submit was refused, the page now edits that draft."""
    life = _lifecycle(request)
    target = page.target
    page.problem = problem_of(error)
    if error.details.get("exists") and target.is_new:
        page.problem = _exists(target.kind, object_id)
    elif error.details.get("changed"):
        page.problem = problem_of(
            error, link=f"/edit/{target.kind}/{object_id.partition(':')[2]}",
            link_text="Open the latest version (copy your text from the box first)",
        )  # fmt: skip
    if saved is not None:
        name = object_id.partition(":")[2]
        page.target = Target(target.kind, name)
        page.expected = saved.proposal_sha256 or page.expected
        page.stored = "The draft was saved, but it was not submitted."
    page.source = await _source(life, context.caller, object_id)
    if page.source is not None:
        page.diff = await _diff(page.source.official_text, page.text, page.source.version)
    _log(action, context, object_id, str(error.code))
    return _render(request, page, status_of(error))


# --- the routes of the three actions ---------------------------------------------------------


def _register() -> None:
    """`POST .../check`, `.../save` and `.../submit` for `/edit/<kind>/<name>` and `/new/<kind>`.
    Each is one route that does one thing; the buttons of the form name them with `formaction`,
    so the page works without script."""

    def edit(action: str) -> Callable[..., Awaitable[Response]]:
        async def handler(
            request: Request,
            kind: str,
            name: str,
            yaml_text: Annotated[str, Form()] = "",
            reason: Annotated[str, Form()] = "",
            expected: Annotated[str, Form()] = "",
        ) -> Response:
            _guard(request)
            return await _post(request, _target(kind, name), action, yaml_text, reason, expected)

        return handler

    def new(action: str) -> Callable[..., Awaitable[Response]]:
        async def handler(
            request: Request,
            kind: str,
            yaml_text: Annotated[str, Form()] = "",
            reason: Annotated[str, Form()] = "",
        ) -> Response:
            _guard(request)
            return await _post(request, _target(kind), action, yaml_text, reason, NEW_OBJECT)

        return handler

    async def withdraw(
        request: Request,
        kind: str,
        name: str,
        reason: Annotated[str, Form()] = "",
        expected: Annotated[str, Form()] = "",
    ) -> Response:
        _guard(request)
        return await _withdraw(request, _target(kind, name), reason, expected)

    editor_router.add_api_route("/edit/{kind}/{name}/withdraw", withdraw, methods=["POST"])
    for action in ("check", "save", "submit"):
        editor_router.add_api_route(
            f"/edit/{{kind}}/{{name}}/{action}", edit(action), methods=["POST"]
        )
        editor_router.add_api_route(f"/new/{{kind}}/{action}", new(action), methods=["POST"])


_register()
