"""WP-19 fix round 1: the definition parser stays fast on the worst text the editor accepts, an
integer that is too long is a V01 issue and not an exception, and the decode cache never hands
out a shared object."""

from __future__ import annotations

import itertools
import string
import time
from pathlib import Path

import pytest
import yaml

from makna.definitions.loader import load_text
from makna.definitions.render import MAX_YAML_BYTES, parse_text
from makna.model.definitions import _unique
from makna.services.store import _decode, _DecodeCache

ROOT = Path(__file__).resolve().parents[2]
BASE = """\
apiVersion: makna/v1
kind: metric
name: contract_count_total
label: Total Contract Count
description: The number of contracts in the portfolio on the last snapshot.
domain: lending
owner: {team: credit-risk}
status: draft
sensitivity: internal
synonyms:
  en: [PLACEHOLDER]
  id: [total kontrak]
type: simple
measure: contract_snapshot.active_contracts
format: {kind: number, decimals: 0}
"""


def worst_text() -> str:
    """A valid metric of just under 256 KiB whose synonym list holds about 44,000 unique words."""
    alphabet = string.ascii_lowercase + string.digits
    words: list[str] = []
    size = 0
    budget = MAX_YAML_BYTES - len(BASE) - 64
    for length in range(5):
        for letters in itertools.product(alphabet, repeat=length):
            word = "x" + "".join(letters)
            if size + len(word) + 2 > budget:
                return BASE.replace("PLACEHOLDER", ", ".join(words))
            words.append(word)
            size += len(word) + 2
    raise AssertionError("not enough words")


def test_the_worst_synonym_text_parses_in_well_under_a_second_or_two() -> None:
    text = worst_text()
    assert MAX_YAML_BYTES - 200 < len(text.encode()) <= MAX_YAML_BYTES
    started = time.perf_counter()
    definition, problems = parse_text(text)
    elapsed = time.perf_counter() - started
    assert definition is not None and hasattr(definition, "synonyms"), problems
    assert definition.synonyms is not None and len(definition.synonyms["en"]) > 30000
    assert elapsed < 2.0, elapsed  # it took about 10 s when `_unique` was quadratic


def test_unique_is_linear_and_says_the_same_thing() -> None:
    started = time.perf_counter()
    assert len(_unique([str(i) for i in range(200_000)])) == 200_000
    assert time.perf_counter() - started < 1.0
    with pytest.raises(ValueError, match=r"items must be unique, 'b' appears twice"):
        _unique(["a", "b", "c", "b"])
    with pytest.raises(ValueError, match=r"items must be unique, \[1\] appears twice"):
        _unique([[1], [2], [1]])  # an item that cannot be hashed is still compared
    assert _unique([[1], [2]]) == [[1], [2]]


def test_an_integer_of_too_many_digits_is_a_v01_issue_with_its_line() -> None:
    digits = "9" * 5000
    for body in (f"a: {digits}\n", f"name: x\nlabel: y\nvalue: {digits}\n", f"- {digits}\n"):
        file, issues = load_text("x.yaml", body)
        assert file.definition is None
        assert [i.code for i in issues] == ["V01"]
        assert "too many digits" in issues[0].message
        assert (
            issues[0].line
            == body.splitlines().index(next(r for r in body.splitlines() if digits in r)) + 1
        )
    # parse_text (the editor and the lifecycle) gets the same answer, not an exception.
    definition, problems = parse_text(BASE.replace("PLACEHOLDER", "a") + f"extra: {digits}\n")
    assert definition is None and [p.code for p in problems] == ["V01"]


def test_a_cache_hit_is_a_copy_so_nobody_can_change_what_later_reads_get() -> None:
    data = BASE.replace("PLACEHOLDER", "alpha, beta").encode()
    path = "lending/metrics/contract_count_total.draft.yaml"
    first, issues = _decode(path, data)
    assert first.definition is not None and issues == []
    first.definition.label = "changed by a caller"
    second, _ = _decode(path, data)
    third, _ = _decode(path, data)
    assert second.definition is not None and third.definition is not None
    assert second.definition.label == "Total Contract Count"
    assert second.definition is not third.definition
    second.definition.label = "changed again"
    assert third.definition.label == "Total Contract Count"
    # Another path with the same bytes is another entry; other bytes are another entry.
    other, _ = _decode(path, data.replace(b"alpha", b"gamma"))
    assert other.definition is not None and other.definition.synonyms["en"] == ["gamma", "beta"]  # type: ignore[index]


def test_the_cache_is_bounded() -> None:
    cache = _DecodeCache(entries=3, max_bytes=1000)
    file, issues = load_text("a.yaml", yaml.safe_dump({"a": 1}))
    for i in range(10):
        cache.put((f"p{i}", "h"), 10, file, issues)
    assert len(cache._items) == 3
    cache.put(("big", "h"), 900, file, issues)  # over a quarter of the budget: not kept
    assert cache.get(("big", "h")) is None
    cache.put(("m", "h"), 240, file, issues)
    cache.put(("n", "h"), 240, file, issues)
    cache.put(("o", "h"), 240, file, issues)
    assert cache._bytes <= 1000


@pytest.mark.parametrize(
    "value",
    ["9" * 5000, "0x" + "f" * 4000, "0" + "7" * 5000, "0b" + "1" * 20000, "-0x" + "f" * 4000],
)
def test_an_integer_over_64_bits_in_any_base_is_a_v01_issue_with_its_line(value: str) -> None:
    body = BASE.replace("PLACEHOLDER", "a") + (
        f"filters:\n  - {{dimension: contract_snapshot.branch, op: '=', value: {value}}}\n"
    )
    line = body.splitlines().index(next(r for r in body.splitlines() if value in r)) + 1
    file, issues = load_text("x.yaml", body)
    assert file.definition is None and issues
    assert {i.code for i in issues} == {"V01"} and line in {i.line for i in issues}


def test_the_largest_integers_the_model_allows_are_64_bit() -> None:
    for number in (2**63 - 1, -(2**63), 0, 7):
        body = BASE.replace("PLACEHOLDER", "a") + (
            f"filters:\n  - {{dimension: contract_snapshot.branch, op: '=', value: {number}}}\n"
        )
        definition, problems = parse_text(body)
        assert definition is not None, problems
    # Past 64 bits a whole number is read as a float; past what a float holds it is refused.
    for number in (10**400, -(10**400)):
        body = BASE.replace("PLACEHOLDER", "a") + (
            f"filters:\n  - {{dimension: contract_snapshot.branch, op: '=', value: {number}}}\n"
        )
        assert parse_text(body)[0] is None


def test_a_filter_constant_is_never_a_reason_for_sync_to_raise() -> None:
    from makna.index.indexer import _scalar_text

    assert _scalar_text(7) == "7" and _scalar_text("a") == "a"
    assert _scalar_text(int("9" * 4000)) == "9" * 4000
    import sys

    sys.set_int_max_str_digits(4300)
    assert _scalar_text(10**5000) == "<number>"


def test_the_decode_cache_counts_the_estimated_memory_not_the_file_bytes() -> None:
    from makna.services import store

    assert store.PARSED_BYTES_PER_FILE_BYTE == 40
    assert store.DECODE_CACHE_BYTES == 256 * 1024 * 1024
    cache = store._DECODED
    before = cache._bytes
    data = BASE.replace("PLACEHOLDER", "unique-for-this-test-7f3a").encode()
    _decode("lending/metrics/contract_count_total.draft.yaml", data)
    assert cache._bytes - before == len(data) * store.PARSED_BYTES_PER_FILE_BYTE
    # Twenty files of the largest size (one user at the cap) and the sample's official set fit.
    sample = sum(p.stat().st_size for p in (ROOT / "semantic" / "definitions").rglob("*.yaml"))
    assert (20 * MAX_YAML_BYTES + sample) * 40 < store.DECODE_CACHE_BYTES
