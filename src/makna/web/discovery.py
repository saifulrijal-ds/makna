"""The discovery pages of WP-17 (WEB-1): the start page, search, domains, and the pages of metrics,
entities, and glossary terms. They only read.

Every read goes through `Services` with the signed-in `Caller` (channel `web`): `search`,
`get_definition`, `list_related`, and the page reads of `Services.discovery`. Nothing here queries a
table, and nothing here builds SQL or a file path from a value of the request. A name in a path is
accepted only when it has the shape of an object name (`makna.services.objects.ID_PATTERN`);
anything else, an unknown name, and an object the caller may not see are the same fixed 404 page.

Stored text (descriptions, caveats, synonyms, example questions) is free text written by curators.
The templates are autoescaped and no value is marked safe. Every link is built here, from a name
that passed the pattern or with `urlencode`, and handed to the template as a finished path.

**The search partial.** `/search?q=` answers with the whole page; the same route answers with only
the results fragment (`_search_results.html`) when the request carries the `HX-Request` header,
which HTMX adds. `Vary: HX-Request` tells a cache the two differ. The input of the page asks for
the fragment as you type (`hx-get` with a delay), so no script of ours is needed and the content
security policy stays `script-src 'self'`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from makna.services import Caller, ServiceError, ServiceErrorCode, permissions
from makna.services.discovery import Freshness
from makna.services.objects import ID_PATTERN, MAX_ID_LENGTH
from makna.services.views import DefinitionView, SearchHit
from makna.web.routes import _db, _signed_in, _state

router = APIRouter()

SEARCH_KINDS = ("metric", "glossary_term", "entity")
"""What the search page looks for: the kinds that have a page here."""
SEARCH_LIMIT = 10
PAGE_PREFIX = {
    "metric": "/metrics",
    "entity": "/entities",
    "glossary_term": "/glossary",
    "domain": "/domains",
}
KIND_LABEL = {
    "metric": "Metric",
    "entity": "Entity",
    "glossary_term": "Glossary term",
    "domain": "Domain",
}
GROUP_TITLE = {"metric": "Metrics", "entity": "Entities", "glossary_term": "Glossary terms"}
NOT_AVAILABLE = "freshness not available"
INVALID_SEARCH = "The search text cannot be used. Use plain words, at most 500 characters."


# --- links ------------------------------------------------------------------------------------


def valid_name(kind: str, name: str) -> bool:
    """True when `name` has the shape of an object name (never a path, never a query)."""
    return len(name) <= MAX_ID_LENGTH and ID_PATTERN.fullmatch(f"{kind}:{name}") is not None


def page_path(object_id: str) -> str | None:
    """The page of an object id, or None when the id has no page here or is not well formed. A
    dimension or rule is shown on the page of its entity."""
    kind, _, name = object_id.partition(":")
    if kind in ("dimension", "rule"):
        kind, name = "entity", name.partition(".")[0]
    prefix = PAGE_PREFIX.get(kind)
    if prefix is None or not valid_name(kind, name):
        return None
    return f"{prefix}/{quote(name, safe='')}"


def bench_path(question: str) -> str:
    """The test bench with a question typed in; the question is URL-encoded."""
    return "/bench?" + urlencode({"q": question})


# --- freshness text ---------------------------------------------------------------------------


def ago(then: datetime, now: datetime) -> str:
    """How long ago, in words: "just now", "5 minutes ago", "2 hours ago", "3 days ago"."""
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"  # pragma: no cover - the loop returns for 60 seconds and more


def freshness_text(freshness: Freshness, now: datetime | None = None) -> str:
    """ "data up to 2026-06-30, checked 2 hours ago", or "freshness not available" when no date is
    stored. A metric that reads several measures says its date is the earliest of them."""
    if freshness.latest is None:
        return NOT_AVAILABLE
    text = f"data up to {freshness.latest.isoformat()}"
    if freshness.checked_at is not None:
        checked = freshness.checked_at
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        text += f", checked {ago(checked, now or datetime.now(UTC))}"
    if freshness.measures > 1:
        text += f" (the earliest of {freshness.measures} measures)"
    return text


# --- what the pages show ----------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    text: str
    href: str | None


def _hit_row(hit: SearchHit) -> dict[str, Any]:
    return {
        "hit": hit,
        "href": page_path(hit.id),
        "kind": KIND_LABEL.get(hit.kind, hit.kind),
        "status": (hit.status or "approved").replace("_", " "),
    }


def _listed_row(item: Any) -> dict[str, Any]:
    return {
        "item": item,
        "href": page_path(item.id),
        "status": (item.status or "approved").replace("_", " "),
    }


# --- routes -----------------------------------------------------------------------------------


@router.get("/")
async def home(request: Request) -> Response:
    context = _signed_in(request)
    discovery = _state(request).services.discovery
    domains = await _db(discovery.domains, context.caller)
    recent = await _db(discovery.recently_approved, context.caller)
    return _state(request).pages.render(
        request.scope,
        "home.html",
        {
            "domains": [
                {
                    "domain": d,
                    "href": page_path(f"domain:{d.name}"),
                    "counts": [(GROUP_TITLE[k], n) for k, n in d.counts.items()],
                }
                for d in domains
            ],
            "recent": [_listed_row(item) for item in recent],
        },
    )


@router.get("/search")
async def search(request: Request, q: str = "") -> Response:
    context = _signed_in(request)
    state = _state(request)
    text = q.strip()
    partial = request.headers.get("hx-request", "").lower() == "true"
    values: dict[str, Any] = {"q": text, "results": [], "outcome": None, "error": None}
    status = 200
    if text:
        try:
            outcome = await _db(
                state.services.search,
                context.caller,
                text,
                kinds=SEARCH_KINDS,
                limit=SEARCH_LIMIT,
            )
        except ServiceError as error:
            # Both texts are fixed: the first is ours, the second the service's own for INTERNAL.
            if error.code == ServiceErrorCode.INVALID_INPUT:
                values["error"], values["q"], status = INVALID_SEARCH, "", 400
            elif error.code == ServiceErrorCode.INTERNAL:
                values["error"], status = error.message, 503
            else:
                raise
        else:
            values["outcome"] = outcome
            values["results"] = [_hit_row(hit) for hit in outcome.hits]
    response = state.pages.render(
        request.scope,
        "_search_results.html" if partial else "search.html",
        values,
        status=status,
    )
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/domains/{name}")
async def domain(request: Request, name: str) -> Response:
    context = _signed_in(request)
    state = _state(request)
    if not valid_name("domain", name):
        raise HTTPException(status_code=404)
    listing = await _db(state.services.discovery.domain, context.caller, name)
    if listing is None:
        raise HTTPException(status_code=404)
    return state.pages.render(
        request.scope,
        "domain.html",
        {
            "domain": listing,
            "groups": [
                (GROUP_TITLE[kind], [_listed_row(item) for item in items])
                for kind, items in listing.groups.items()
            ],
        },
    )


async def _definition(
    request: Request, kind: str, name: str, *, detail: str
) -> tuple[Caller, DefinitionView, list[dict[str, Any]]]:
    """The definition and what is related to it, or the 404 page: for a name that is malformed,
    unknown, or not visible to the caller, which are the same answer."""
    context = _signed_in(request)
    if not valid_name(kind, name):
        raise HTTPException(status_code=404)
    services = _state(request).services
    object_id = f"{kind}:{name}"
    try:
        view = await _db(services.get_definition, context.caller, object_id, detail=detail)
        related = await _db(services.list_related, context.caller, object_id)
    except ServiceError as error:
        if error.code == ServiceErrorCode.NOT_FOUND:
            raise HTTPException(status_code=404) from None
        if error.code == ServiceErrorCode.PERMISSION_DENIED:
            raise HTTPException(status_code=403) from None
        raise
    rows = [
        {
            "item": item,
            "href": page_path(item.id),
            "kind": KIND_LABEL.get(item.kind, item.kind),
            "relation": item.relation.replace("_", " "),
        }
        for item in related.related
    ]
    return context.caller, view, rows


def _object_values(
    caller: Caller, view: DefinitionView, related: list[dict[str, Any]]
) -> dict[str, Any]:
    replaced_by = view.deprecation.get("replaced_by")
    return {
        "view": view,
        "related": related,
        "curator": permissions.can_see_drafts(caller),
        "status": (view.status or "approved").replace("_", " "),
        "deprecation": (
            {
                "reason": view.deprecation.get("reason", ""),
                "replacement": Link(replaced_by, page_path(replaced_by)) if replaced_by else None,
            }
            if view.deprecation
            else None
        ),
    }


@router.get("/metrics/{name}")
async def metric(request: Request, name: str) -> Response:
    caller, view, related = await _definition(request, "metric", name, detail="brief")
    state = _state(request)
    freshness = await _db(state.services.discovery.freshness, name)
    values = _object_values(caller, view, related)
    values["freshness"] = freshness_text(freshness)
    values["examples"] = [(q, bench_path(q)) for q in view.example_questions]
    return state.pages.render(request.scope, "metric.html", values)


@router.get("/entities/{name}")
async def entity(request: Request, name: str) -> Response:
    caller, view, related = await _definition(request, "entity", name, detail="full")
    values = _object_values(caller, view, related)
    values["relationships"] = [
        (item, page_path(f"entity:{item.to}")) for item in view.relationships
    ]
    return _state(request).pages.render(request.scope, "entity.html", values)


@router.get("/glossary/{name}")
async def glossary(request: Request, name: str) -> Response:
    caller, view, related = await _definition(request, "glossary_term", name, detail="full")
    values = _object_values(caller, view, related)
    values["terms"] = [(item, page_path(item.id)) for item in view.related]
    return _state(request).pages.render(request.scope, "glossary.html", values)
