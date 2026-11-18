"""The run record and the runs file (docs/EVALS.md sections 5.3 to 5.5).

A runs file (`evals/reports/agent/<run-name>/runs.jsonl`) holds one JSON object per line, one line
per finished run. The harness (WP-22B) appends each run as soon as it ends; this module defines the
line (`RunRecord`), writes one (`dump_line`) and reads a file (`read_runs`).

Every line carries a `conditions` string: a hash of the model id, the prompt, the tools and the
golden set the run was made under (`conditions_hash`). A file whose lines do not all carry the same
conditions (or the same model id and prompt hash) is refused: a report never mixes conditions
(section 5.5). A line that does not validate is an error that names its line number and the
field, never the text of the line (a tool result can hold personal data).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

RUN_SCHEMA = "makna-eval-agent-run/v1"
ARMS = ("M", "R")
MAX_PROBLEMS = 20
"""A runs file with more bad lines than this names the first ones and says how many more."""


class RunsFileError(Exception):
    """The runs file cannot be used: it is missing, a line is invalid, or it mixes conditions."""


class MixedConditionsError(RunsFileError):
    """The lines were not made under one set of conditions (docs/EVALS.md section 5.5)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


class ToolCall(_Strict):
    name: Annotated[str, StringConstraints(min_length=1)]
    arguments: dict[str, Any]
    result: str
    """The text the tool returned to the model."""


class TokenCounts(_Strict):
    input: Annotated[int, Field(ge=0)]
    output: Annotated[int, Field(ge=0)]


class RunRecord(_Strict):
    """One run of one question by one arm (`schema` in the file)."""

    schema_id: Literal["makna-eval-agent-run/v1"] = Field(alias="schema")
    question: Annotated[str, StringConstraints(pattern=r"^G\d{2,}$")]
    arm: Literal["M", "R"]
    run: Annotated[int, Field(ge=0)]
    """Run index within (question, arm): 0 to 2 in a full run."""
    model_id: Annotated[str, StringConstraints(min_length=1)]
    prompt_hash: Annotated[str, StringConstraints(min_length=1)]
    conditions: Annotated[str, StringConstraints(min_length=1)]
    tool_calls: list[ToolCall]
    tokens: TokenCounts
    reply: str
    """The final reply text of the agent, as received."""
    harness_error: str = ""
    """Empty when the run ended normally; else what stopped it (never a personal value)."""

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.question, self.arm, self.run)


def conditions_hash(*, model_id: str, prompt_hash: str, tools_hash: str, golden_hash: str) -> str:
    """The `conditions` of a run: equal exactly when model id, prompt, tools and golden set are."""
    payload = json.dumps(
        [model_id, prompt_hash, tools_hash, golden_hash], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def dump_line(record: RunRecord) -> str:
    """The JSON line of a record (no trailing newline)."""
    return json.dumps(record.model_dump(by_alias=True), ensure_ascii=False, separators=(",", ":"))


def _describe(error: ValidationError) -> str:
    # `detail["input"]` is left out on purpose: it can be a tool result.
    return "; ".join(
        f"{'.'.join(str(part) for part in d['loc']) or 'line'}: {d['msg']}" for d in error.errors()
    )


def parse_lines(lines: Iterable[str]) -> list[RunRecord]:
    """The records of a runs file's lines. Raises `RunsFileError` listing every bad line (by
    number) and `MixedConditionsError` when the lines come from different conditions."""
    records: list[RunRecord] = []
    problems: list[str] = []
    first_seen: dict[tuple[str, str, int], int] = {}
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            record = RunRecord.model_validate_json(line)
        except ValidationError as error:
            problems.append(f"line {number}: {_describe(error)}")
            continue
        if record.key in first_seen:
            problems.append(
                f"line {number}: run {record.key[0]}/{record.key[1]}/{record.key[2]} "
                f"already on line {first_seen[record.key]}"
            )
            continue
        first_seen[record.key] = number
        records.append(record)
    if problems:
        shown = problems[:MAX_PROBLEMS]
        if len(problems) > MAX_PROBLEMS:
            shown.append(f"and {len(problems) - MAX_PROBLEMS} more")
        raise RunsFileError("invalid runs file: " + "; ".join(shown))
    check_conditions(records)
    return records


def check_conditions(records: Iterable[RunRecord]) -> None:
    """Raises `MixedConditionsError` unless one `conditions`, model id and prompt hash cover all."""
    rows = list(records)
    for field in ("conditions", "model_id", "prompt_hash"):
        counts = Counter(getattr(r, field) for r in rows)
        if len(counts) > 1:
            listing = ", ".join(f"{value} ({n} runs)" for value, n in counts.most_common())
            raise MixedConditionsError(
                f"the runs file mixes conditions: {len(counts)} different values of {field}: "
                f"{listing}. A report compares runs made under one model, prompt, tool set and "
                "golden set; use a new run name for other conditions."
            )


def read_runs(path: Path) -> list[RunRecord]:
    """Read and validate a runs file. Raises `RunsFileError` (also for a file with no run)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RunsFileError(f"cannot read {path}: {type(error).__name__}") from None
    records = parse_lines(text.splitlines())
    if not records:
        raise RunsFileError(f"{path}: the runs file holds no run")
    return records
