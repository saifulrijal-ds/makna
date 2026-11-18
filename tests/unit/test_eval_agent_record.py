"""WP-22: the run record and the runs file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.agent.record import (
    MixedConditionsError,
    RunRecord,
    RunsFileError,
    conditions_hash,
    dump_line,
    parse_lines,
    read_runs,
)

from .agent_support import make_record, reply_text


def line(**kwargs: object) -> str:
    return dump_line(make_record("G01", "M", 0, reply_text("refused"), **kwargs))  # type: ignore[arg-type]


def test_a_record_round_trips_through_its_line() -> None:
    record = make_record("G01", "R", 2, reply_text("refused"), tool_results=["a", "b"])
    text = dump_line(record)
    assert json.loads(text)["schema"] == "makna-eval-agent-run/v1"
    assert RunRecord.model_validate_json(text) == record
    assert "\n" not in text


def test_the_conditions_hash_changes_with_each_part() -> None:
    base = {"model_id": "m", "prompt_hash": "p", "tools_hash": "t", "golden_hash": "g"}
    seen = {conditions_hash(**base)}
    for key in base:
        seen.add(conditions_hash(**{**base, key: "other"}))
    assert len(seen) == 5


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        json.dumps({"schema": "makna-eval-agent-run/v2"}),
    ],
)
def test_an_invalid_line_is_an_error_naming_its_number(bad: str) -> None:
    with pytest.raises(RunsFileError, match="line 2"):
        parse_lines([line(), bad])


def test_every_bad_line_is_named_and_the_error_never_repeats_the_line() -> None:
    good = json.loads(line())
    good["arm"] = "Z"
    good["reply"] = "SECRET-TEXT"
    other = json.loads(line())
    del other["tokens"]
    with pytest.raises(RunsFileError) as raised:
        parse_lines([json.dumps(good), "", json.dumps(other)])
    message = str(raised.value)
    assert "line 1: arm" in message and "line 3: tokens" in message
    assert "SECRET-TEXT" not in message


def test_a_negative_token_count_and_an_unknown_field_are_invalid() -> None:
    record = json.loads(line())
    record["tokens"]["input"] = -1
    with pytest.raises(RunsFileError, match="tokens.input"):
        parse_lines([json.dumps(record)])
    record = json.loads(line())
    record["extra"] = 1
    with pytest.raises(RunsFileError, match="extra"):
        parse_lines([json.dumps(record)])


def test_a_run_twice_in_a_file_is_an_error() -> None:
    with pytest.raises(RunsFileError, match="line 2: run G01/M/0 already on line 1"):
        parse_lines([line(), line()])


def test_mixed_conditions_are_refused_with_a_clear_message() -> None:
    first = dump_line(make_record("G01", "M", 0, reply_text("refused")))
    other = dump_line(make_record("G02", "M", 0, reply_text("refused"), conditions="other"))
    with pytest.raises(MixedConditionsError, match="mixes conditions.*conditions") as raised:
        parse_lines([first, other])
    assert "new run name" in str(raised.value)


def test_a_different_model_id_is_mixed_conditions_too() -> None:
    first = dump_line(make_record("G01", "M", 0, reply_text("refused")))
    other = dump_line(make_record("G02", "M", 0, reply_text("refused"), model_id="fake-model-2"))
    with pytest.raises(MixedConditionsError, match="model_id"):
        parse_lines([first, other])


def test_read_runs_reads_a_file_and_refuses_an_empty_or_missing_one(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_text(line() + "\n\n" + dump_line(make_record("G02", "M", 0, "x")) + "\n")
    assert [r.question for r in read_runs(path)] == ["G01", "G02"]
    path.write_text("\n")
    with pytest.raises(RunsFileError, match="no run"):
        read_runs(path)
    with pytest.raises(RunsFileError, match="cannot read"):
        read_runs(tmp_path / "missing.jsonl")
