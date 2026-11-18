"""WP-22: the reply parser (docs/EVALS.md section 5.2)."""

from __future__ import annotations

import json

import pytest

from evals.agent.reply import parse_reply

from .agent_support import reply_text

OBJECT = reply_text("answered", ["region", "value"], [["Jawa Barat", 0.031]], "NPF", "select 1")


def test_a_bare_object_parses() -> None:
    reply = parse_reply(OBJECT)
    assert reply is not None
    assert reply.status == "answered"
    assert reply.columns == ("region", "value")
    assert reply.rows == (("Jawa Barat", 0.031),)
    assert (reply.interpretation, reply.sql, reply.has_answer) == ("NPF", "select 1", True)


@pytest.mark.parametrize(
    "text",
    [
        f"```json\n{OBJECT}\n```",
        f"Here is the result.\n\n{OBJECT}\n\nLet me know.",
        f"Braces {{like this}} in prose first. {OBJECT}",
        f'{{"status": "refused"}}\n{OBJECT}',
        f"```\n{OBJECT}\n```\nDone.",
    ],
)
def test_an_object_in_a_fence_or_in_prose_is_found(text: str) -> None:
    reply = parse_reply(text)
    assert reply is not None and reply.status == "answered"


def test_the_last_top_level_object_is_the_reply() -> None:
    text = f"{OBJECT}\nCorrection: {reply_text('no_governed_answer')}"
    reply = parse_reply(text)
    assert reply is not None and reply.status == "no_governed_answer"
    assert not reply.has_answer


def test_nested_objects_are_not_top_level() -> None:
    nested = json.dumps({"status": "answered", "answer": {"columns": ["a"], "rows": [[1]]}})
    reply = parse_reply(nested)
    assert reply is not None and reply.status == "answered"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "[1, 2, 3]",
        OBJECT[:-5],  # truncated
        '{"status": "done"}',
        '{"status": "Answered"}',
        '{"status": 1}',
        '{"answer": {"columns": [], "rows": []}}',
        f"{OBJECT}\n" + '{"note": "last object has no status"}',
        '{"status": "answered"}',  # answered needs an answer
        '{"status": "answered", "answer": "text"}',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [[1, 2]]}}',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [1]}}',
        '{"status": "answered", "answer": {"columns": [1], "rows": []}}',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [[[1]]]}}',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [[NaN]]}}',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [[Infinity]]}}',
        '{"status": "refused", "interpretation": 5}',
        '{"status": "refused", "sql": ["select 1"]}',
        '{"status": "refused", "answer": 3}',
        "{'status': 'refused'}",
    ],
)
def test_anything_else_is_unparseable(text: str) -> None:
    assert parse_reply(text) is None


def test_the_reply_is_data_never_code() -> None:
    assert parse_reply('{"status": "refused", "sql": "__import__(\'os\')"}') is not None
    assert parse_reply('{"status": __import__("os")}') is None


def test_other_statuses_need_no_answer_and_null_fields_are_empty() -> None:
    reply = parse_reply('{"status": "needs_clarification", "answer": null, "interpretation": null}')
    assert reply is not None
    assert (reply.interpretation, reply.sql, reply.has_answer) == ("", "", False)


@pytest.mark.parametrize(
    "text",
    [
        '[{"status": "refused"}]',
        'See: [{"status": "refused"}] done',
        '{"status": "answered", "answer": {"columns": ["a"], "rows": [[1]]}, "x": [',
        '{"outer": [1, {"status": "refused"}',
        '{"status": "refused"} then {"status": "answered", "answer": {"columns": ["a"], "rows"',
        '{"status": "refused"} and then an open [ bracket',
    ],
)
def test_an_object_in_an_array_or_in_a_truncated_object_is_not_the_reply(text: str) -> None:
    assert parse_reply(text) is None


def test_a_balanced_object_after_balanced_prose_brackets_is_found() -> None:
    reply = parse_reply('Note [1] and {braces} first. {"status": "refused"} Fin (see [1]).')
    assert reply is not None and reply.status == "refused"
    quoted = parse_reply('He said "use { here and {"status": "refused"}')
    assert quoted is None  # the stray { opens an object that never closes


def test_a_deeply_nested_reply_is_unparseable_not_a_crash() -> None:
    assert parse_reply('{"a":' * 100_000) is None
    assert parse_reply('{"a":[1,2,3,' * 30_000) is None
    assert parse_reply('{"a":' * 50_000 + "1" + "}" * 50_000) is None  # balanced, too deep
    deep = '{"status": "refused", "x": ' + "[" * 100_000 + "]" * 100_000 + "}"
    assert parse_reply(deep) is None
    assert parse_reply("[" * 100_000 + OBJECT) is None


def test_a_flood_of_open_braces_is_fast() -> None:
    import time

    start = time.perf_counter()
    assert parse_reply("{" * 200_000) is None
    assert parse_reply("{ " * 100_000 + OBJECT) is None
    assert parse_reply("{x}" * 100_000 + OBJECT) is not None
    assert time.perf_counter() - start < 3.0


def test_only_the_end_of_a_huge_reply_is_read() -> None:
    from evals.agent.reply import MAX_TEXT

    assert parse_reply("x" * (2 * MAX_TEXT) + OBJECT) is not None
    assert parse_reply(OBJECT + "x" * (2 * MAX_TEXT)) is None
