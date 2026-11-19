"""Rendering: the Jinja environment, the navigation, and the fixed error pages.

Templates are in `templates/` and are autoescaped. Pages refer to the application by path
(`/account`, `/static/...`), never by an absolute URL, so a page makes no request to any other
host. An error page carries fixed text chosen by the status: no exception text, no path, no
value the request held.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from starlette.responses import HTMLResponse
from starlette.types import Scope

from makna.services import Caller, permissions
from makna.web.security import Session

TEMPLATES = Path(__file__).parent / "templates"
SCOPE_KEY = "makna.web"
"""The ASGI scope key under which the gate stores the `WebContext` of a signed-in request."""

CSRF_FAILED = "csrf"

_ERROR_TEXT: Mapping[int | str, tuple[str, str]] = {
    400: ("Bad request", "The request could not be understood."),
    403: ("Not allowed", "You do not have access to this page."),
    CSRF_FAILED: (
        "Request not verified",
        "The request could not be verified. Go back, reload the page, and try again.",
    ),
    404: ("Not found", "This page does not exist."),
    405: ("Not allowed", "This page does not accept that kind of request."),
    413: ("Too large", "The request is too large."),
    500: ("Something went wrong", "Makna could not complete the request. Try again later."),
    503: (
        "Not available",
        "Makna cannot reach its user store right now. Try again in a little while.",
    ),
}
_FALLBACK_TEXT = ("Something went wrong", "The request could not be completed.")


@dataclass(frozen=True)
class WebContext:
    """Who a request is from: the session of its cookie and the `Caller` the user store gave for
    it on this request."""

    session: Session
    caller: Caller


def web_context(scope: Scope) -> WebContext | None:
    found = scope.get(SCOPE_KEY)
    return found if isinstance(found, WebContext) else None


def navigation(caller: Caller | None) -> list[tuple[str, str]]:
    """The links a caller sees: (label, path). Only pages that exist, and the administration
    pages only for a caller who may manage."""
    if caller is None:
        return []
    links = [("Home", "/"), ("Search", "/search")]
    if permissions.can_use_test_bench(caller):
        links.append(("Bench", "/bench"))
    if permissions.can_edit_proposals(caller):
        links.append(("New", "/new"))
    links.append(("Account", "/account"))
    if permissions.can_manage(caller):
        links += [("Users", "/admin/users"), ("Tokens", "/admin/tokens")]
    return links


class Pages:
    """The template environment of one application."""

    def __init__(self) -> None:
        self._env = Environment(
            loader=FileSystemLoader(TEMPLATES),
            autoescape=True,
            auto_reload=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        scope: Scope,
        template: str,
        context: Mapping[str, Any] | None = None,
        *,
        status: int = 200,
    ) -> HTMLResponse:
        web = web_context(scope)
        caller = web.caller if web else None
        values: dict[str, Any] = {
            "caller": caller,
            "csrf_token": web.session.csrf if web else "",
            "nav": navigation(caller),
        }
        values.update(context or {})
        text = self._env.get_template(template).render(values)
        return HTMLResponse(text, status_code=status)

    def error(self, scope: Scope, status: int, kind: int | str | None = None) -> HTMLResponse:
        """The error page of `status`. `kind` picks other fixed text for the same status."""
        title, message = _ERROR_TEXT.get(kind if kind is not None else status, _FALLBACK_TEXT)
        return self.render(
            scope,
            "error.html",
            {"status": status, "title": title, "message": message},
            status=status,
        )
