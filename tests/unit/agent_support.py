"""Hand-made pieces for the agent eval tests: run records, replies and a small scan."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import cache
from typing import Any

from evals.agent.golden import Golden, load_golden
from evals.agent.record import RunRecord, TokenCounts, ToolCall, conditions_hash
from evals.agent.scan import PersonalScan

MODEL = "fake-model-1"
PROMPT = "prompt-hash-1"
CONDITIONS = conditions_hash(
    model_id=MODEL, prompt_hash=PROMPT, tools_hash="tools-1", golden_hash="golden-1"
)


@cache
def golden() -> Golden:
    return load_golden()


def reply_text(
    status: str,
    columns: Sequence[str] | None = None,
    rows: Sequence[Sequence[Any]] = (),
    interpretation: str = "",
    sql: str = "",
) -> str:
    body: dict[str, Any] = {"status": status}
    if columns is not None:
        body["answer"] = {"columns": list(columns), "rows": [list(r) for r in rows]}
    if interpretation:
        body["interpretation"] = interpretation
    if sql:
        body["sql"] = sql
    return json.dumps(body)


def make_record(
    question: str,
    arm: str,
    run: int,
    reply: str,
    *,
    tool_results: Sequence[str] = (),
    tool_calls: int | None = None,
    tokens: tuple[int, int] = (100, 20),
    harness_error: str = "",
    conditions: str = CONDITIONS,
    model_id: str = MODEL,
) -> RunRecord:
    calls = [
        ToolCall(name="run_sql", arguments={"sql": "select 1"}, result=result)
        for result in tool_results
    ]
    if tool_calls is not None:
        calls = [ToolCall(name="list_tables", arguments={}, result="[]")] * tool_calls
    return RunRecord.model_validate(
        {
            "schema": "makna-eval-agent-run/v1",
            "question": question,
            "arm": arm,
            "run": run,
            "model_id": model_id,
            "prompt_hash": PROMPT,
            "conditions": conditions,
            "tool_calls": [c.model_dump() for c in calls],
            "tokens": TokenCounts(input=tokens[0], output=tokens[1]).model_dump(),
            "reply": reply,
            "harness_error": harness_error,
        }
    )


def small_scan() -> PersonalScan:
    """Three invented customers; no real data."""
    return PersonalScan(
        {
            "full_name": ["Alpha Beta", "Gamma Delta", "Epsilon Zeta"],
            "national_id": ["9912345678000001", "9987654321000002", "9955500011000003"],
            "phone_number": ["0899000000001", "0899000000002", "0899000000003"],
            "email": ["alpha.beta1@example.com", "gamma.delta2@example.com", "e.z3@example.com"],
            "birth_date": ["1990-01-02", "1985-05-06", "1979-07-08"],
        }
    )
