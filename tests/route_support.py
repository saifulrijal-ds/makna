"""Listing the routes of an application, for the tests that must see every one of them."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI


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
