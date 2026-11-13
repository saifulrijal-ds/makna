"""The four services: `search`, `get_definition`, `list_related`, `compile_query` (WP-12).

`Services` holds the database engine, the embedding provider, the snapshot cache, and the
governance policy. Every method takes a `Caller` first, checks the permission of section 11,
validates its input before any work, and writes one `tool_call_log` row whatever the outcome.

- **Answers** are typed views (`makna.services.views`), or the compiler's `CompileResponse`.
- **Refusals** are `ServiceError` (`NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_INPUT`,
  `CATALOG_NOT_SYNCED`) or the compiler's `QueryError`, which passes through with its own code.
- **Anything else** that goes wrong becomes `ServiceError(INTERNAL)` with a fixed message: no
  exception text leaves a service. A `GovernanceError` is the one exception that is understood:
  a metric that reads a personal column where that is not allowed is `BROKEN_REFERENCE` with the
  issue `R05` (ARCHITECTURE 5.3).

No service runs SQL on the engine or accepts SQL text (D-21): `compile_query` returns the SQL of
a structured request and nothing more.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import Engine

from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.compiler.snapshot import Model
from makna.model.definitions import NAME_PATTERN, Entity, Metric
from makna.model.governance import GovernancePolicy
from makna.model.request import CompileResponse, ErrorCode, QueryError, QueryRequest
from makna.model.search import EmbeddingProvider, SearchSettings
from makna.search.confidence import LOW_GUIDANCE
from makna.search.embedding import EmbeddingError
from makna.search.hybrid import HybridSearch, SearchError
from makna.services import objects, permissions
from makna.services.caller import Caller
from makna.services.calllog import CallLog, CallState
from makna.services.definitions import DefinitionReader
from makna.services.derive import personal_column_issue
from makna.services.errors import ServiceError, ServiceErrorCode, invalid_input
from makna.services.related import RELATIONS, list_related
from makna.services.snapshot import Snapshot, SnapshotCache
from makna.services.views import (
    DefinitionView,
    RelatedOutcome,
    SearchHit,
    SearchOutcome,
    cut,
)

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 500
MAX_LIMIT = 10
DEFAULT_LIMIT = 5
SEARCH_KINDS = ("metric", "glossary_term", "entity", "dimension", "column")
DETAILS = ("brief", "full")
SUPPORTED_DIALECTS = ("postgres", "duckdb")

INTERNAL_MESSAGE = (
    "Something went wrong in Makna and the request could not be answered. "
    "Try again; if it keeps happening, ask an administrator to check the server log."
)
NOT_SYNCED_MESSAGE = (
    "No catalog has been synced, so Makna cannot compile queries: column names are checked "
    "only against the catalog. Ask an administrator to run `makna catalog sync`."
)
SEARCH_UNAVAILABLE_MESSAGE = (
    "The search index is not ready. Ask an administrator to run `makna index build`."
)
KINDS_GUIDANCE = (
    "No governed definition clearly matches among the kinds searched ({kinds}). Tell the user "
    "that no approved definition was found for this; do not approximate it with a different "
    "one."
)


class Services:
    """The service layer of one process. Methods are safe to call from several threads."""

    def __init__(
        self,
        engine: Engine,
        provider: EmbeddingProvider,
        *,
        policy: GovernancePolicy,
        dialect: str = "postgres",
        search_settings: SearchSettings | None = None,
    ) -> None:
        if dialect not in SUPPORTED_DIALECTS:
            raise ValueError(
                f"dialect must be one of {', '.join(SUPPORTED_DIALECTS)}, not {dialect!r}"
            )
        self._engine = engine
        self._dialect: Literal["postgres", "duckdb"] = (
            "duckdb" if dialect == "duckdb" else "postgres"
        )
        self.policy = policy
        self._search = HybridSearch(engine, provider, search_settings)
        self._cache = SnapshotCache(engine)
        self._calls = CallLog(engine)
        self._warned: set[tuple[str, str]] = set()
        self._warn_lock = threading.Lock()
        self._reader = DefinitionReader(policy, self._warn_stale_policy)

    @property
    def snapshots(self) -> SnapshotCache:
        return self._cache

    def dispose(self) -> None:
        """Release the database connections."""
        self._engine.dispose()

    # --- logging and errors --------------------------------------------------------------

    def _warn_stale_policy(self, stored: str, running: str) -> None:
        """One warning per pair: the index holds dimensions made under another policy."""
        with self._warn_lock:
            if (stored, running) in self._warned:
                return
            self._warned.add((stored, running))
        logger.warning(
            "the index holds available dimensions computed under another governance policy "
            "(stored %s, running %s); they are computed from the snapshot instead. Run "
            "`makna index build` to store them again.",
            stored,
            running,
        )

    @contextmanager
    def _logged(self, caller: Caller, tool: str) -> Iterator[CallState]:
        """Time the call and write its `tool_call_log` row, whatever happens in the body."""
        call = CallState(caller, tool)
        try:
            yield call
        except ServiceError as error:
            call.fail(
                "error" if error.code == ServiceErrorCode.INTERNAL else "refused", str(error.code)
            )
            raise
        except QueryError as error:
            call.fail("refused", str(error.code))
            raise
        except Exception as error:
            logger.error("service call failed: tool=%s error=%s", tool, type(error).__name__)
            call.fail("error", str(ServiceErrorCode.INTERNAL))
            raise ServiceError(ServiceErrorCode.INTERNAL, INTERNAL_MESSAGE) from None
        else:
            call.status = "ok"
        finally:
            self._calls.write(call)

    # --- search --------------------------------------------------------------------------

    def search(
        self,
        caller: Caller,
        query: str,
        *,
        kinds: Collection[str] | None = None,
        domain: str | None = None,
        include_deprecated: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> SearchOutcome:
        """Find objects by meaning. Viewers and tokens see approved objects only (deprecated
        ones when `include_deprecated`); curators and above also see drafts and proposals."""
        with self._logged(caller, "search") as call:
            call.request = CallLog.search_request(query, kinds, domain, include_deprecated, limit)
            text = self._check_search(query, kinds, domain, include_deprecated, limit)
            permissions.require(caller, permissions.Permission.READ_APPROVED)
            role = "curator" if permissions.can_see_drafts(caller) else "viewer"
            chosen = list(kinds) if kinds else None
            try:
                found = self._search.search(
                    text,
                    role=role,
                    kinds=chosen,
                    domain=domain,
                    include_deprecated=include_deprecated,
                    limit=limit,
                )
            except (SearchError, EmbeddingError) as error:
                logger.warning("search is not available: %s", type(error).__name__)
                raise ServiceError(ServiceErrorCode.INTERNAL, SEARCH_UNAVAILABLE_MESSAGE) from None
            with self._engine.connect() as connection:
                rows = objects.fetch_objects(
                    connection, [_owner_id(h.object_id) for h in found.hits]
                )
            hits = [_search_hit(hit, rows) for hit in found.hits]
            guidance = found.confidence.guidance
            if guidance == LOW_GUIDANCE and chosen and "metric" not in chosen:
                guidance = KINDS_GUIDANCE.format(kinds=", ".join(chosen))
            outcome = SearchOutcome(confidence=found.confidence.band, guidance=guidance, hits=hits)
            call.result = {"ids": [hit.id for hit in hits], "confidence": outcome.confidence}
            return outcome

    @staticmethod
    def _check_search(
        query: object,
        kinds: object,
        domain: object,
        include_deprecated: object,
        limit: object,
    ) -> str:
        if not isinstance(query, str) or not query.strip():
            raise invalid_input("query", "give the words to search for; it must not be empty")
        if len(query) > MAX_QUERY_LENGTH:
            raise invalid_input(
                "query", f"at most {MAX_QUERY_LENGTH} characters", maximum=MAX_QUERY_LENGTH
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise invalid_input("limit", f"a whole number from 1 to {MAX_LIMIT}", maximum=MAX_LIMIT)
        if kinds is not None:
            allowed = list(SEARCH_KINDS)
            if (
                isinstance(kinds, str)
                or not isinstance(kinds, (list, tuple, set, frozenset))
                or not kinds
                or any(k not in allowed for k in kinds)
            ):
                raise invalid_input(
                    "kinds", "a non-empty list of kinds from the allowed list", allowed=allowed
                )
        if domain is not None and (
            not isinstance(domain, str) or not re.fullmatch(NAME_PATTERN, domain)
        ):
            raise invalid_input("domain", "a domain name such as lending")
        if not isinstance(include_deprecated, bool):
            raise invalid_input("include_deprecated", "true or false")
        return query.strip()

    # --- get_definition ------------------------------------------------------------------

    def get_definition(self, caller: Caller, id: str, *, detail: str = "full") -> DefinitionView:
        """The full definition of one object by id. An object the caller may not see is
        `NOT_FOUND`, exactly like an id that does not exist."""
        with self._logged(caller, "get_definition") as call:
            call.request = CallLog.id_request(id, detail=detail)
            self._check_id(id)
            if detail not in DETAILS:
                raise invalid_input("detail", "brief or full", allowed=list(DETAILS))
            permissions.require(caller, permissions.Permission.READ_APPROVED)
            sees_drafts = permissions.can_see_drafts(caller)
            snapshot, state = self._cache.current()
            with self._engine.connect() as connection:
                view = self._reader.read(
                    connection, snapshot, state, id, sees_drafts=sees_drafts, detail=detail
                )
                if view is None:
                    raise _not_found(connection, id, sees_drafts)
            call.result = {"ids": [view.id]}
            return view

    @staticmethod
    def _check_id(object_id: object) -> None:
        if not isinstance(object_id, str) or not object_id:
            raise invalid_input("id", "an id of the form kind:name, for example metric:npf_ratio")
        if len(object_id) > objects.MAX_ID_LENGTH:
            raise invalid_input(
                "id", f"at most {objects.MAX_ID_LENGTH} characters", maximum=objects.MAX_ID_LENGTH
            )
        if objects.ID_PATTERN.fullmatch(object_id) is None:
            raise invalid_input(
                "id",
                "an id of the form kind:name, for example metric:npf_ratio",
                kinds=list(objects.ID_KINDS),
            )

    # --- list_related --------------------------------------------------------------------

    def list_related(
        self,
        caller: Caller,
        id: str,
        *,
        relation: str | None = None,
        include_deprecated: bool = False,
    ) -> RelatedOutcome:
        """Up to 20 objects related to one object, with one-line descriptions. Objects the
        caller may not see are never listed."""
        with self._logged(caller, "list_related") as call:
            call.request = CallLog.id_request(
                id, relation=relation, include_deprecated=include_deprecated
            )
            self._check_id(id)
            if relation is not None and relation not in RELATIONS:
                raise invalid_input(
                    "relation", "one of the allowed relations", allowed=list(RELATIONS)
                )
            if not isinstance(include_deprecated, bool):
                raise invalid_input("include_deprecated", "true or false")
            permissions.require(caller, permissions.Permission.READ_APPROVED)
            sees_drafts = permissions.can_see_drafts(caller)
            snapshot, state = self._cache.current()
            with self._engine.connect() as connection:
                if (
                    self._reader.read(
                        connection, snapshot, state, id, sees_drafts=sees_drafts, detail="brief"
                    )
                    is None
                ):
                    raise _not_found(connection, id, sees_drafts)
                items = list_related(
                    connection,
                    id,
                    sees_drafts=sees_drafts,
                    include_deprecated=include_deprecated,
                    relation=relation,
                )
            call.result = {"ids": [item.id for item in items]}
            return RelatedOutcome(related=items)

    # --- compile_query -------------------------------------------------------------------

    def compile_query(
        self, caller: Caller, request: QueryRequest | Mapping[str, Any]
    ) -> CompileResponse:
        """Turn a structured request into SQL for the configured dialect, using only the
        approved snapshot, whatever the caller's role. Nothing is run."""
        with self._logged(caller, "compile_query") as call:
            snapshot: Snapshot | None = None
            parsed: QueryRequest | None = None
            try:
                call.request = CallLog.compile_request(request, None)
                parsed = self._parse_request(request)
                permissions.require(caller, permissions.Permission.COMPILE)
                snapshot, state = self._cache.current()
                call.request = CallLog.compile_request(parsed, snapshot.model)
                if not state.catalog_synced:
                    raise ServiceError(ServiceErrorCode.CATALOG_NOT_SYNCED, NOT_SYNCED_MESSAGE)
                response = self._compile(caller, snapshot, parsed)
            except QueryError as error:
                if error.code == ErrorCode.PERSONAL_DATA_REFUSED:
                    call.request = CallLog.refusal_request(
                        parsed, snapshot.model if snapshot is not None else None
                    )
                raise
            call.result = {"ids": [response.metric.id], "dialect": response.dialect}
            return response

    @staticmethod
    def _parse_request(request: object) -> QueryRequest:
        if isinstance(request, QueryRequest):
            return request
        if not isinstance(request, Mapping):
            raise invalid_input("request", "a structured request: a metric and its options")
        try:
            return QueryRequest.model_validate(dict(request))
        except ValidationError as error:
            fields = sorted(
                {".".join(str(p) for p in e["loc"]) or "request" for e in error.errors()}
            )
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The request does not have the shape of section 7.1: check the fields "
                f"{', '.join(fields)}.",
                {"fields": fields},
            ) from None

    def _compile(
        self, caller: Caller, snapshot: Snapshot, request: QueryRequest
    ) -> CompileResponse:
        model = snapshot.model
        if request.metric not in model.metrics:
            self._refuse_unapproved(caller, request.metric)
        else:
            _refuse_broken_metric(model, snapshot, request.metric)
        try:
            return compile_request(
                model, request, self._dialect, enforce_status=True, governance=self.policy
            )
        except GovernanceError as error:
            raise _personal_column_error(error, model, request.metric) from None

    def _refuse_unapproved(self, caller: Caller, metric: str) -> None:
        """A curator who asks for a draft learns it is not approved; everyone else gets the
        compiler's `UNKNOWN_METRIC`, which is the answer for an id that does not exist."""
        if not permissions.can_see_drafts(caller):
            return
        with self._engine.connect() as connection:
            row = objects.fetch_object(connection, f"metric:{metric}")
        if row is not None and row.status in ("draft", "in_review"):
            raise QueryError(
                ErrorCode.NOT_APPROVED,
                f"Metric {metric} is {row.status.replace('_', ' ')} and cannot be used until it "
                "is approved. Choose an approved metric.",
                {"metric": metric, "status": row.status},
            )


# --- helpers --------------------------------------------------------------------------------


def _owner_id(object_id: str) -> str:
    kind, _, name = object_id.partition(":")
    return f"entity:{name.partition('.')[0]}" if kind in ("dimension", "rule") else object_id


def _search_hit(hit: Any, rows: Mapping[str, objects.ObjectRow]) -> SearchHit:
    kind = hit.kind
    row = rows.get(_owner_id(hit.object_id))
    label = hit.object_id.partition(":")[2]
    type_: str | None = None
    unit: str | None = None
    if row is not None and hit.object_id == row.id:
        label = row.label
        if kind == "metric":
            served = row.definition
            if isinstance(served, Metric):
                type_ = served.type
                unit = served.format.unit or (
                    "percent" if served.format.kind == "percent" else None
                )
    else:
        label = _part_label(hit, row)
    return SearchHit(
        id=hit.object_id,
        label=label,
        kind=kind,
        description=cut(hit.description),
        matched_terms=list(hit.matched_terms),
        type=type_,
        unit=unit,
        status=None if hit.status == "approved" else hit.status,
    )


def _part_label(hit: Any, row: objects.ObjectRow | None) -> str:
    """The label of a dimension, rule, column, or catalog term hit: the label the definition
    gives it, else the first of the document's names (its name with spaces)."""
    first = str(hit.names.split(" ; ")[0])
    if hit.kind not in ("dimension", "rule") or row is None:
        return first
    entity = row.definition
    if not isinstance(entity, Entity):
        return first
    part = hit.object_id.partition(":")[2].partition(".")[2]
    found: Any = None
    if hit.kind == "dimension":
        found = next((d for d in entity.dimensions if d.name == part), None)
    else:
        found = next((r for r in entity.rules or [] if r.name == part), None)
    return str(found.label) if found is not None and found.label else first


def _not_found(connection: Any, object_id: str, sees_drafts: bool) -> ServiceError:
    close = objects.close_ids(connection, object_id, sees_drafts)
    return ServiceError(
        ServiceErrorCode.NOT_FOUND,
        f"There is no object with the id {object_id}."
        + (f" Did you mean {', '.join(close)}?" if close else "")
        + " Search for it and use the id exactly as the hit gives it.",
        {"id": object_id, "close_ids": close},
    )


def _metric_closure(model: Model, name: str) -> list[Metric]:
    found: list[Metric] = []
    stack = [name]
    while stack:
        current = model.metrics.get(stack.pop())
        if current is None or any(m.name == current.name for m in found):
            continue
        found.append(current)
        stack.extend(
            n for n in (current.numerator, current.denominator, *(current.inputs or [])) if n
        )
    return found


def _refuse_broken_metric(model: Model, snapshot: Snapshot, name: str) -> None:
    """`BROKEN_REFERENCE` for a metric (or an input) with an open `R05` issue, before any
    planning (ARCHITECTURE 5.3)."""
    problems = [
        {"code": issue.code, "object": f"metric:{member.name}", "message": issue.message}
        for member in _metric_closure(model, name)
        for issue in snapshot.metric_issues.get(member.name, ())
    ]
    if problems:
        raise _broken(problems)


def _broken(issues: list[dict[str, str]]) -> QueryError:
    """`BROKEN_REFERENCE` for `R05` issues: the message says what a curator must change."""
    return QueryError(
        ErrorCode.BROKEN_REFERENCE,
        "The definitions this metric depends on are broken: "
        + "; ".join(i["message"] for i in issues)
        + ". A curator must change the definition or the catalog tags.",
        {"issues": issues},
    )


def _personal_column_error(error: GovernanceError, model: Model, metric: str) -> Exception:
    """The structured refusal for a `GovernanceError` about a personal column, or `INTERNAL`
    for any other kind. The internal reason goes to the application log as names only."""
    if error.table is None or error.column is None:
        logger.warning(
            "compile: the final check failed for metric %s (%s)", metric, type(error).__name__
        )
        return ServiceError(ServiceErrorCode.INTERNAL, INTERNAL_MESSAGE)
    logger.warning(
        "compile: metric %s reads personal column %s of table %s where that is not allowed",
        metric,
        error.column,
        error.table,
    )
    _, message = personal_column_issue(error.table, error.column, model.derived_sensitivity)
    return _broken([{"code": "R05", "object": f"metric:{metric}", "message": message}])
