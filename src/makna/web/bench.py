"""The test bench (WEB-4, ARCHITECTURE section 13): `/bench`.

`GET /bench?q=...` resolves a question by rules (`Services.resolve`) and, when it resolved to one
metric, compiles the request it built (`Services.compile_query`). The page shows, in order: the
question box, the search hits with their scores, the resolution, the request as editable JSON, and
the SQL with its explanation. `POST /bench` takes an edited request, parses it into a
`QueryRequest` (a field it does not know is refused), and compiles it again with the signed-in
caller; a JSON problem, a validation problem, and a compile error are shown with their code and
details.

The bench never runs SQL (D-21): it imports nothing from the engine package and has no route or
button that executes the compiled text. A GET changes no definition, user, or setting; the calls
it makes are logged like any other (ARCHITECTURE section 14).

The only clock read of the whole feature is `today()` below, at the edge: the resolver takes the
date as a parameter and the page shows it.
"""

from __future__ import annotations

import datetime
import functools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

import anyio.to_thread
from fastapi import APIRouter, Form, HTTPException, Request
from pydantic import ValidationError
from starlette.responses import Response

from makna.model.request import CompileResponse, QueryError, QueryRequest
from makna.services import Caller, Resolution, Services, permissions
from makna.services.errors import ServiceError
from makna.web.pages import Pages, WebContext, web_context

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16 * 1024
"""The size of the request JSON the bench accepts: 16 KiB. A request of the compile API is a few
hundred bytes; this leaves room for long filter lists."""
SKELETON = '{\n  "metric": ""\n}'
"""What the request box holds when the resolver chose no metric."""

bench_router = APIRouter()


def today() -> datetime.date:
    """The date the bench counts as today: the server's date, read once per request."""
    return datetime.date.today()


@dataclass(frozen=True)
class Failure:
    """An error to show: its code, its message, and its details as JSON text."""

    code: str
    message: str
    details: str


@dataclass
class Bench:
    """Everything the page shows."""

    question: str
    today: datetime.date
    resolution: Resolution | None = None
    resolve_error: Failure | None = None
    request_text: str = SKELETON
    compiled: CompileResponse | None = None
    compile_error: Failure | None = None
    input_error: str | None = None


def _failure(error: QueryError | ServiceError) -> Failure:
    details = json.dumps(error.details, indent=2, ensure_ascii=False, default=str, sort_keys=True)
    return Failure(str(error.code), error.message, details if error.details else "")


def _dump(request: QueryRequest) -> str:
    return json.dumps(
        request.model_dump(mode="json", exclude_defaults=True), indent=2, ensure_ascii=False
    )


def _services(request: Request) -> Services:
    services = request.app.state.web.services
    assert isinstance(services, Services)
    return services


def _pages(request: Request) -> Pages:
    pages = request.app.state.web.pages
    assert isinstance(pages, Pages)
    return pages


def _signed_in(request: Request) -> WebContext:
    context = web_context(request.scope)
    if context is None or not permissions.can_use_test_bench(context.caller):
        raise HTTPException(status_code=403)
    return context


async def _db[T](function: Callable[..., T], *args: Any) -> T:
    """Run a call that reads the database off the event loop."""
    return await anyio.to_thread.run_sync(functools.partial(function, *args))


# --- the work, in a worker thread ------------------------------------------------------------


def _resolve(services: Services, caller: Caller, view: Bench) -> None:
    if not view.question:
        return
    try:
        view.resolution = services.resolve(caller, view.question, view.today)
    except (ServiceError, QueryError) as error:
        view.resolve_error = _failure(error)


def _compile(services: Services, caller: Caller, view: Bench, request: QueryRequest) -> None:
    try:
        view.compiled = services.compile_query(caller, request)
    except (ServiceError, QueryError) as error:
        view.compile_error = _failure(error)


def _read(question: str, caller: Caller, services: Services, day: datetime.date) -> Bench:
    """A GET: resolve, and compile what the resolver built."""
    view = Bench(question, day)
    _resolve(services, caller, view)
    resolution = view.resolution
    if resolution is not None and resolution.request is not None:
        view.request_text = _dump(resolution.request)
        _compile(services, caller, view, resolution.request)
    return view


def parse_request(text: str) -> tuple[QueryRequest | None, str | None]:
    """The request a text of JSON stands for, or the reason it does not.

    The reason names the place and the rule, never the text that was sent. A field that
    `QueryRequest` does not have is refused.
    """
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        return None, f"The request is longer than {MAX_REQUEST_BYTES // 1024} KiB."
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        place = f"line {error.lineno}, column {error.colno}"
        return None, f"This is not valid JSON: {error.msg} ({place})."
    except RecursionError:
        return None, "This is not a request: the JSON is nested too deeply."
    if not isinstance(data, dict):
        return None, 'The request must be a JSON object such as {"metric": "npf_ratio"}.'
    try:
        return QueryRequest.model_validate(data), None
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in item['loc']) or 'request'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        ]
        return None, "The request does not have the shape of section 7.1. " + "; ".join(problems)


def _submit(
    text: str, question: str, caller: Caller, services: Services, day: datetime.date
) -> Bench:
    """A POST: the question is resolved again for the blocks above, and the edited request is the
    one that is compiled."""
    view = Bench(question, day, request_text=text)
    _resolve(services, caller, view)
    parsed, problem = parse_request(text)
    if parsed is None:
        view.input_error = problem
    else:
        _compile(services, caller, view, parsed)
    return view


# --- the routes ------------------------------------------------------------------------------


def _page(request: Request, view: Bench, status: int = 200) -> Response:
    return _pages(request).render(
        request.scope,
        "bench.html",
        {
            "question": view.question,
            "today": view.today,
            "resolution": view.resolution,
            "resolve_error": view.resolve_error,
            "request_text": view.request_text,
            "compiled": view.compiled,
            "compile_error": view.compile_error,
            "input_error": view.input_error,
            "max_request_bytes": MAX_REQUEST_BYTES,
        },
        status=status,
    )


@bench_router.get("/bench")
async def bench(request: Request, q: str = "") -> Response:
    context = _signed_in(request)
    question = q.strip()
    day = today()
    view = (
        await _db(_read, question, context.caller, _services(request), day)
        if question
        else Bench(question, day)
    )
    return _page(request, view)


@bench_router.post("/bench")
async def bench_compile(
    request: Request,
    request_json: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
) -> Response:
    context = _signed_in(request)
    question = q.strip()
    if len(request_json.encode("utf-8")) > MAX_REQUEST_BYTES:
        # The text is not echoed back: the box keeps the skeleton and the page says why.
        view = Bench(question, today())
        view.input_error = f"The request is longer than {MAX_REQUEST_BYTES // 1024} KiB."
        return _page(request, view, status=413)
    view = await _db(_submit, request_json, question, context.caller, _services(request), today())
    return _page(request, view, status=400 if view.input_error else 200)
