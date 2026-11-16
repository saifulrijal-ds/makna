"""Shared pieces of the WP-16 integration tests: the real application on a scratch database,
a browser-like client, users with random passwords, and the sign-in flow.

The passwords are random for each run and never written to a file.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.engine import URL

from makna.app import create_app
from makna.config import Settings
from makna.services import accounts

HOST = "testserver"
SECRET_KEY = secrets.token_hex(32)
PASSWORD = secrets.token_urlsafe(18)
"""The password of every user these tests create (random for each run)."""

_CSRF_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_settings(
    url: URL | str, *, development: bool = False, secret_key: str = SECRET_KEY
) -> Settings:
    text = url if isinstance(url, str) else url.render_as_string(hide_password=False)
    return Settings.model_validate(
        {
            "database_url": text,
            "secret_key": secret_key,
            "embedding_provider": "hash",
            "mcp": {"allowed_hosts": [HOST]},
            "web": {"development": development},
        }
    )


def make_app(url: URL | str, *, development: bool = False) -> FastAPI:
    return create_app(make_settings(url, development=development))


def make_client(app: FastAPI, *, development: bool = False) -> TestClient:
    """A client that keeps cookies and never follows a redirect. Over `https` unless this is the
    development stack, because a `Secure` cookie is not sent back over plain `http`."""
    scheme = "http" if development else "https"
    return TestClient(
        app, base_url=f"{scheme}://{HOST}", follow_redirects=False, raise_server_exceptions=False
    )


_counter = iter(range(1, 1_000_000))


def make_user(
    engine: Engine,
    role: str = "viewer",
    *,
    teams: tuple[str, ...] = (),
    name: str = "Test User",
) -> accounts.User:
    """A new active user with a unique email and the password `PASSWORD`."""
    email = f"user{next(_counter)}-{secrets.token_hex(3)}@demo.example"
    return accounts.create_user(engine, email, name, role, teams, PASSWORD)


def csrf_in(html: str) -> str:
    found = _CSRF_FIELD.search(html)
    assert found, "no csrf_token field in the page"
    return found.group(1)


def sign_in(client: TestClient, email: str, password: str = PASSWORD, next_: str = "") -> Any:
    """Load the form, then post it: the response of the POST."""
    page = client.get("/login" + (f"?next={next_}" if next_ else ""))
    assert page.status_code == 200
    return client.post(
        "/login",
        data={
            "csrf_token": csrf_in(page.text),
            "email": email,
            "password": password,
            "next": next_,
        },
    )


def signed_in(app: FastAPI, user: accounts.User, *, development: bool = False) -> TestClient:
    client = make_client(app, development=development)
    response = sign_in(client, user.email)
    assert response.status_code == 303, response.text
    return client


def page_csrf(client: TestClient, path: str = "/account") -> str:
    response = client.get(path)
    assert response.status_code == 200
    return csrf_in(response.text)


def iter_routes(app: FastAPI) -> Iterator[tuple[str, frozenset[str]]]:
    """Every (path, methods) of the application, including routes of included routers, which
    FastAPI wraps in a private class. A route nobody wrapped is listed as it is."""

    def walk(routes: list[Any], prefix: str) -> Iterator[tuple[str, frozenset[str]]]:
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                extra = getattr(getattr(route, "include_context", None), "prefix", "")
                yield from walk(list(inner.routes), prefix + extra)
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            methods = getattr(route, "methods", None) or {"GET"}
            yield prefix + path, frozenset(methods)

    yield from walk(list(app.routes), "")


def fill(path: str) -> str:
    """A concrete path for a route with parameters."""
    return re.sub(r"\{[^}]+\}", "1", path)


def plant(client: TestClient, name: str, value: str) -> None:
    """Put a cookie into the client's jar as the browser would hold it for the application, so a
    cookie the application sets later replaces it instead of sitting beside it."""
    client.cookies.set(name, value, domain=f"{HOST}.local", path="/")
