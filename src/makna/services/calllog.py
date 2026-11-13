"""The tool-call log (ARCHITECTURE section 14, GOV-2, D-24).

One row per service call, on every outcome, written in its own transaction after the call so a
failed call still leaves its row. A call that has an answer does not keep it when its row cannot
be written: `write` says whether it was, and the service turns a failure into `INTERNAL`
(ARCHITECTURE section 14: no answer without a log row). Building the request of the row never
fails the call: if redacting or serialising it raises, the row holds `{"unloggable": true}`.

What a row holds:

- `channel`, `user_id`, `token_id` from the `Caller`; the `tool`; `status` (`ok`; `refused` for a
  structured error other than `INTERNAL`; `error` for `INTERNAL`); `error_code`; `duration_ms`.
- `request`: the input after redaction (`makna.services.redact`). Every string is clipped and
  redacted. In a compile request the value of a filter is kept only when its dimension is known
  to be non-personal in the approved snapshot of a known metric; otherwise it is
  `[REDACTED]`, whatever the outcome. A call refused with `PERSONAL_DATA_REFUSED` stores no free
  text: only the metric name, dimension refs, and filter dimension refs the model knows.
- `result`: the ids returned, the confidence band, and the error code. Never SQL text,
  descriptions, or values: Makna never sees result rows.
"""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Engine, insert
from sqlalchemy.exc import IntegrityError

from makna.compiler.snapshot import Model
from makna.index.tables import tool_call_log
from makna.model.request import QueryRequest
from makna.services.caller import Caller
from makna.services.redact import REDACTED, redact_value

logger = logging.getLogger(__name__)

INVALID = "[invalid]"
UNLOGGABLE: dict[str, Any] = {"unloggable": True}
FIELD_UNLOGGABLE = "[unloggable]"
ID_LIMIT = 200
MAX_LIST = 50


def _text(value: object, limit: int) -> Any:
    """A string, clipped and redacted; None stays None; anything else is marked invalid."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return redact_value(value, limit)
        except Exception:
            return FIELD_UNLOGGABLE  # this field only; the others are kept
    return INVALID


def _texts(value: object, limit: int) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_text(item, limit) for item in list(value)[:MAX_LIST]]
    return INVALID


def _flag(value: object) -> Any:
    return value if isinstance(value, bool) else INVALID


def _whole(value: object) -> Any:
    return value if isinstance(value, int) and not isinstance(value, bool) else INVALID


def _storable(built: dict[str, Any]) -> dict[str, Any]:
    """`built` with every field the JSON column cannot take replaced by `[unloggable]`, field by
    field (and filter by filter), so one bad field does not cost the others."""

    def fine(value: Any) -> bool:
        try:
            json.dumps(value, allow_nan=False)
            return True
        except Exception:
            return False

    kept: dict[str, Any] = {}
    for key, value in built.items():
        if key == "filters" and isinstance(value, list):
            kept[key] = [item if fine(item) else FIELD_UNLOGGABLE for item in value]
        else:
            kept[key] = value if fine(value) else FIELD_UNLOGGABLE
    return kept


def _never_fails(build: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Wrap a request builder so that nothing it does can fail the call."""

    @functools.wraps(build)
    def safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return _storable(build(*args, **kwargs))
        except Exception:
            return dict(UNLOGGABLE)

    return safe


class CallState:
    """What one call knows about itself while it runs; `CallLog.write` stores it."""

    def __init__(self, caller: Caller, tool: str) -> None:
        self.caller = caller
        self.tool = tool
        self.started = time.perf_counter()
        self.request: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.status = "error"
        self.error_code: str | None = None

    def fail(self, status: str, code: str) -> None:
        self.status = status
        self.error_code = code
        self.result = {"error_code": code}

    @property
    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)


class CallLog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def write(self, call: CallState) -> bool:
        """Store the row in its own transaction. Returns whether it was stored; a failure is
        logged (the class only) and never raised. If the request cannot be stored, the row is
        stored once more with `{"unloggable": true}` in its place, keeping tool, status and
        error code."""
        request = call.request
        for attempt in (request, UNLOGGABLE):
            if attempt is UNLOGGABLE and request is None:
                break
            try:
                with self._engine.begin() as connection:
                    connection.execute(
                        insert(tool_call_log).values(
                            channel=call.caller.channel,
                            user_id=call.caller.user_id,
                            token_id=call.caller.token_id,
                            tool=call.tool,
                            request=attempt,
                            result=call.result,
                            status=call.status,
                            error_code=call.error_code,
                            duration_ms=call.duration_ms,
                        )
                    )
                return True
            except Exception as error:
                # The class only: an error text can hold the row's parameters, the query too.
                logger.error(
                    "tool_call_log write failed: tool=%s error=%s", call.tool, type(error).__name__
                )
                if isinstance(error, IntegrityError):
                    break  # the user or token is gone: another request cannot help
        return False

    # --- what each tool records of its input -------------------------------------------------

    @staticmethod
    @_never_fails
    def search_request(
        query: object,
        kinds: object,
        domain: object,
        include_deprecated: object,
        limit: object,
    ) -> dict[str, Any]:
        return {
            "query": _text(query, 500),
            "kinds": _texts(kinds, 40),
            "domain": _text(domain, 80),
            "include_deprecated": _flag(include_deprecated),
            "limit": _whole(limit),
        }

    @staticmethod
    @_never_fails
    def id_request(object_id: object, **others: object) -> dict[str, Any]:
        request: dict[str, Any] = {"id": _text(object_id, ID_LIMIT)}
        for name, value in others.items():
            if isinstance(value, bool):
                request[name] = value
            else:
                request[name] = _text(value, 40)
        return request

    @staticmethod
    @_never_fails
    def compile_request(request: object, model: Model | None) -> dict[str, Any]:
        """The compile request with every filter value that is not known to be non-personal
        replaced. `model` is the approved snapshot, or None when there is none yet (then every
        value is replaced)."""
        if isinstance(request, QueryRequest):
            data: Mapping[str, Any] = request.model_dump(mode="json")
        elif isinstance(request, Mapping):
            data = request
        else:
            return {"request": INVALID}
        metric = data.get("metric")
        metric_known = model is not None and isinstance(metric, str) and metric in model.metrics
        filters: list[Any] = []
        raw_filters = data.get("filters")
        for item in (raw_filters if isinstance(raw_filters, (list, tuple)) else [])[:MAX_LIST]:
            if not isinstance(item, Mapping):
                filters.append(REDACTED)
                continue
            dimension = item.get("dimension")
            entry: dict[str, Any] = {
                "dimension": _text(dimension, ID_LIMIT),
                "op": _text(item.get("op"), 20),
            }
            if item.get("value") is not None:
                try:
                    entry["value"] = (
                        redact_value(item["value"])
                        if metric_known and _non_personal(model, dimension)
                        else REDACTED
                    )
                except Exception:
                    entry["value"] = FIELD_UNLOGGABLE
            filters.append(entry)
        return {
            "metric": _text(metric, ID_LIMIT),
            "dimensions": _texts(data.get("dimensions"), ID_LIMIT),
            "filters": filters,
            "grain": _text(data.get("grain"), 40),
            "time_start": _text(data.get("time_start"), 40),
            "time_end": _text(data.get("time_end"), 40),
            "limit": _whole(data.get("limit")) if data.get("limit") is not None else None,
        }

    @staticmethod
    @_never_fails
    def refusal_request(request: QueryRequest | None, model: Model | None) -> dict[str, Any]:
        """The record of a call refused with `PERSONAL_DATA_REFUSED`: names the model knows, and
        nothing else (no values, no grain, no dates, no free text)."""
        if request is None or model is None:
            return {}

        def known_dimension(ref: str) -> str:
            return ref if model.dimension(ref) is not None else REDACTED

        return {
            "metric": request.metric if request.metric in model.metrics else REDACTED,
            "dimensions": [known_dimension(ref) for ref in request.dimensions],
            "filters": [{"dimension": known_dimension(f.dimension)} for f in request.filters],
        }


def _non_personal(model: Model | None, dimension: object) -> bool:
    if model is None or not isinstance(dimension, str):
        return False
    found = model.dimension(dimension)
    if found is None:
        return False
    entity, item = found
    return model.effective_sensitivity(entity, item) != "personal"
