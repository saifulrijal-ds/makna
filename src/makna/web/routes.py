"""The routes of WP-16: sign-in and sign-out, the home page, the user's own account, and the
administration of users and tokens.

Handlers hold form handling only. Who is signed in, and the CSRF check, are the gate's
(`makna.web.gate`), which runs before any of these. Everything that reads or changes users and
tokens goes through `Services.accounts`; a database error is an `AccountStoreError`, answered
with the 503 page by the handler registered in `makna.web.install`.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import anyio.to_thread
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse, Response

from makna.services import ROLES, Caller, Services, permissions
from makna.services.accounts import MIN_PASSWORD_LENGTH, AccountError, AccountStore, User
from makna.web.gate import session_cookie_options
from makna.web.pages import Pages, WebContext, web_context
from makna.web.security import (
    LOGIN_MAX_AGE,
    SESSION_MAX_AGE,
    CookieCodec,
    CookieNames,
    LoginThrottle,
    safe_next,
)

logger = logging.getLogger(__name__)

BAD_LOGIN = "Email or password is incorrect."
THROTTLED = "Too many attempts. Try again in a minute."
MAX_LOGIN_FIELD = 1024
"""Longer email or password text is a failed sign-in without a hash: nobody types that."""

NOTICES = {
    "created": "The user was created.",
    "updated": "The role and teams were saved.",
    "deactivated": "The user was deactivated: no sign-in, and their tokens no longer work.",
    "revoked": "The token was revoked.",
}


@dataclass(frozen=True)
class WebState:
    """What the handlers share, kept on `app.state.web`."""

    services: Services
    codec: CookieCodec
    throttle: LoginThrottle
    pages: Pages
    development: bool

    @property
    def names(self) -> CookieNames:
        return CookieNames.for_mode(self.development)

    @property
    def accounts(self) -> AccountStore:
        return self.services.accounts


def _state(request: Request) -> WebState:
    state = request.app.state.web
    assert isinstance(state, WebState)
    return state


async def _db[T](function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a call that reads the database off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(function, *args, **kwargs))


def _signed_in(request: Request) -> WebContext:
    """The context the gate stored. A route the gate lets through without one is a mistake."""
    context = web_context(request.scope)
    if context is None:
        raise HTTPException(status_code=403)
    return context


def _user_id(caller: Caller) -> int:
    if caller.user_id is None:
        raise HTTPException(status_code=403)
    return caller.user_id


router = APIRouter()


# --- sign-in and sign-out ---------------------------------------------------------------------


def _login_page(
    request: Request,
    secret: str,
    *,
    next_path: str,
    email: str = "",
    error: str | None = None,
    status: int = 200,
) -> Response:
    return _state(request).pages.render(
        request.scope,
        "login.html",
        {"login_csrf": secret, "next": next_path, "email": email, "error": error},
        status=status,
    )


@router.get("/login")
async def login_form(request: Request, next: str = "") -> Response:
    state = _state(request)
    secret = state.codec.load_login(request.cookies.get(state.names.login))
    cookie: str | None = None
    if secret is None:
        cookie, secret = state.codec.issue_login()
    response = _login_page(request, secret, next_path=safe_next(next))
    if cookie is not None:
        response.set_cookie(
            state.names.login,
            cookie,
            max_age=LOGIN_MAX_AGE,
            **session_cookie_options(state.development),
        )
    return response


@router.post("/login")
async def login(
    request: Request,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
) -> Response:
    # The gate has checked the CSRF token against the login cookie, so the cookie is there.
    state = _state(request)
    secret = state.codec.load_login(request.cookies.get(state.names.login)) or ""
    target = safe_next(next)
    ticket = state.throttle.reserve(email)
    if ticket is None:
        # No password check for a refused attempt. The refusal is the same for every email.
        response = _login_page(
            request, secret, next_path=target, email=email, error=THROTTLED, status=429
        )
        response.headers["Retry-After"] = "60"
        return response
    user: User | None = None
    try:
        # An email or password of an absurd length is a failed attempt without a hash.
        if len(email) <= MAX_LOGIN_FIELD and len(password) <= MAX_LOGIN_FIELD:
            user = await _db(state.accounts.verify_password, email, password)
    except BaseException:
        state.throttle.release(email, ticket)  # an outage of the user store is not a failure
        raise
    if user is None:
        # One message and one status for an unknown email, a wrong password, and an inactive user.
        # The reservation stays: it is the failure that counts.
        return _login_page(
            request, secret, next_path=target, email=email, error=BAD_LOGIN, status=401
        )
    state.throttle.release(email, ticket)
    # A new session with a new CSRF secret, whatever cookie the browser sent.
    value, _ = state.codec.issue_session(user.id)
    response = RedirectResponse(target, status_code=303)
    options = session_cookie_options(state.development)
    response.set_cookie(state.names.session, value, max_age=SESSION_MAX_AGE, **options)
    response.delete_cookie(state.names.login, **options)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    state = _state(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(state.names.session, **session_cookie_options(state.development))
    return response


# --- home -------------------------------------------------------------------------------------


@router.get("/")
async def home(request: Request) -> Response:
    _signed_in(request)
    return _state(request).pages.render(request.scope, "home.html")


# --- the user's own account -------------------------------------------------------------------


async def _account_page(
    request: Request,
    *,
    new_token: Any = None,
    error: str | None = None,
    status: int = 200,
    done: str = "",
) -> Response:
    state = _state(request)
    caller = _signed_in(request).caller
    user = await _db(state.accounts.get_user, _user_id(caller))
    if user is None:
        raise HTTPException(status_code=403)
    tokens = await _db(state.accounts.list_tokens, user.email)
    return state.pages.render(
        request.scope,
        "account.html",
        {
            "user": user,
            "tokens": tokens,
            "new_token": new_token,
            "error": error,
            "notice": NOTICES.get(done) if done in ("revoked",) else None,
        },
        status=status,
    )


@router.get("/account")
async def account(request: Request, done: str = "") -> Response:
    return await _account_page(request, done=done)


@router.post("/account/tokens")
async def create_own_token(request: Request, name: Annotated[str, Form()] = "") -> Response:
    state = _state(request)
    caller = _signed_in(request).caller
    user = await _db(state.accounts.get_user, _user_id(caller))
    if user is None:
        raise HTTPException(status_code=403)
    try:
        created = await _db(state.accounts.create_token, user.email, name)
    except AccountError as error:
        return await _account_page(request, error=str(error), status=400)
    logger.info("token created: user=%s token=%s", user.id, created.id)
    # The page that shows the token is the response to this POST: the token is in no URL, no
    # redirect, and no later page.
    return await _account_page(request, new_token=created)


@router.post("/account/tokens/{token_id}/revoke")
async def revoke_own_token(request: Request, token_id: int) -> Response:
    state = _state(request)
    caller = _signed_in(request).caller
    user = await _db(state.accounts.get_user, _user_id(caller))
    if user is None:
        raise HTTPException(status_code=403)
    own = await _db(state.accounts.list_tokens, user.email)
    if token_id not in {token.id for token in own}:
        # Someone else's token and an unknown id look the same.
        raise HTTPException(status_code=404)
    await _db(state.accounts.revoke_token, token_id)
    logger.info("token revoked: user=%s token=%s", user.id, token_id)
    return RedirectResponse("/account?done=revoked", status_code=303)


# --- administration ---------------------------------------------------------------------------


def require_manager(request: Request) -> Caller:
    """Only a caller who may manage users and tokens (`permissions.can_manage`): the admin."""
    context = web_context(request.scope)
    if context is None or not permissions.can_manage(context.caller):
        raise HTTPException(status_code=403)
    return context.caller


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_manager)])


def _teams(text: str) -> list[str]:
    return [piece for piece in text.split(",") if piece.strip()]


async def _users_page(
    request: Request,
    *,
    error: str | None = None,
    status: int = 200,
    form: dict[str, str] | None = None,
    done: str = "",
) -> Response:
    state = _state(request)
    users = await _db(state.accounts.list_users)
    return state.pages.render(
        request.scope,
        "admin_users.html",
        {
            "users": users,
            "roles": ROLES,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "error": error,
            "notice": NOTICES.get(done) if done in ("created", "updated", "deactivated") else None,
            "form": form or {"email": "", "display_name": "", "role": "viewer", "teams": ""},
        },
        status=status,
    )


@admin.get("/users")
async def users(request: Request, done: str = "") -> Response:
    return await _users_page(request, done=done)


@admin.post("/users")
async def create_user(
    request: Request,
    caller: Annotated[Caller, Depends(require_manager)],
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    teams: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    try:
        # The password goes to the account store and nowhere else: it is not logged, not kept,
        # and not part of the page that answers.
        created = await _db(
            _state(request).accounts.create_user, email, display_name, role, _teams(teams), password
        )
    except AccountError as error:
        return await _users_page(
            request,
            error=str(error),
            status=400,
            form={"email": email, "display_name": display_name, "role": role, "teams": teams},
        )
    logger.info("admin: user created: actor=%s user=%s role=%s", caller.user_id, created.id, role)
    return RedirectResponse("/admin/users?done=created", status_code=303)


async def _user_by_id(request: Request, user_id: int) -> User:
    for user in await _db(_state(request).accounts.list_users):
        if user.id == user_id:
            return user
    raise HTTPException(status_code=404)


@admin.post("/users/{user_id}/update")
async def update_user(
    request: Request,
    user_id: int,
    caller: Annotated[Caller, Depends(require_manager)],
    role: Annotated[str, Form()] = "",
    teams: Annotated[str, Form()] = "",
) -> Response:
    target = await _user_by_id(request, user_id)
    if target.id == caller.user_id and role != "admin":
        # An admin who removes their own admin role may leave nobody who can undo it.
        return await _users_page(
            request, error="You cannot remove your own admin role.", status=400
        )
    try:
        await _db(
            _state(request).accounts.update_user,
            target.email,
            role=role or None,
            teams=_teams(teams),
        )
    except AccountError as error:
        return await _users_page(request, error=str(error), status=400)
    logger.info("admin: user changed: actor=%s user=%s", caller.user_id, target.id)
    return RedirectResponse("/admin/users?done=updated", status_code=303)


@admin.post("/users/{user_id}/deactivate")
async def deactivate_user(
    request: Request, user_id: int, caller: Annotated[Caller, Depends(require_manager)]
) -> Response:
    target = await _user_by_id(request, user_id)
    if target.id == caller.user_id:
        return await _users_page(
            request, error="You cannot deactivate your own account.", status=400
        )
    try:
        await _db(_state(request).accounts.deactivate_user, target.email)
    except AccountError as error:
        return await _users_page(request, error=str(error), status=400)
    logger.info("admin: user deactivated: actor=%s user=%s", caller.user_id, target.id)
    return RedirectResponse("/admin/users?done=deactivated", status_code=303)


@admin.get("/tokens")
async def tokens(request: Request, done: str = "") -> Response:
    state = _state(request)
    listed = await _db(state.accounts.list_tokens)
    return state.pages.render(
        request.scope,
        "admin_tokens.html",
        {
            "tokens": listed,
            "error": None,
            "notice": NOTICES.get(done) if done == "revoked" else None,
        },
    )


@admin.post("/tokens/{token_id}/revoke")
async def revoke_token(
    request: Request, token_id: int, caller: Annotated[Caller, Depends(require_manager)]
) -> Response:
    try:
        await _db(_state(request).accounts.revoke_token, token_id)
    except AccountError:
        raise HTTPException(status_code=404) from None
    logger.info("admin: token revoked: actor=%s token=%s", caller.user_id, token_id)
    return RedirectResponse("/admin/tokens?done=revoked", status_code=303)
