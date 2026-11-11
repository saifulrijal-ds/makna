"""WP-01 criterion 4: no secrets in committed environment files (NFR-4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def env_example_problems(text: str) -> list[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = stripped.partition("=")
        if not separator:
            problems.append(f"line {number}: not NAME=: {line!r}")
        elif value.strip():
            problems.append(f"line {number}: {name.strip()} has a value")
    return problems


def env_named_files(paths: list[str]) -> list[str]:
    return [p for p in paths if Path(p).name == ".env"]


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def test_env_example_has_no_values() -> None:
    text = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert env_example_problems(text) == []


def test_env_example_lists_the_documented_variables() -> None:
    names = {
        line.partition("=")[0].strip()
        for line in (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert {
        "MAKNA_DATABASE_URL",
        "MAKNA_SECRET_KEY",
        "MAKNA_DEFINITIONS_REPO",
        "MAKNA_ENGINE_URL",
        "MAKNA_EMBEDDING_PROVIDER",
        "MAKNA_OPENMETADATA_TOKEN",
    } <= names


def test_value_in_env_example_is_detected() -> None:
    assert env_example_problems("# c\nMAKNA_SECRET_KEY=hunter2\nMAKNA_X=\n") == [
        "line 2: MAKNA_SECRET_KEY has a value"
    ]
    assert env_example_problems("MAKNA_X= \n") == []
    assert env_example_problems("MAKNA_X\n") != []


def test_no_tracked_file_is_named_dot_env() -> None:
    assert env_named_files(tracked_files()) == []


def test_a_dot_env_file_is_detected() -> None:
    assert env_named_files(["a/.env", "deploy/.env.example", "x.env"]) == ["a/.env"]
