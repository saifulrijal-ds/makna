"""WP-15: the `makna-eval` command line (`python -m evals`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import cli


def test_the_four_commands_exist_and_agent_is_not_there_yet() -> None:
    assert set(cli.COMMANDS) == {"all", "retrieval", "compile", "freeze"}
    parser = cli.build_parser()
    for command in cli.COMMANDS:
        assert parser.parse_args([command]).command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["agent"])


def test_verbose_gates_and_out_are_options_of_the_evals() -> None:
    args = cli.build_parser().parse_args(
        ["all", "--verbose", "--gates", "g.toml", "--out", "somewhere"]
    )
    assert (args.verbose, args.gates, args.out) == (True, "g.toml", "somewhere")


def test_without_a_database_the_exit_code_is_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    assert cli.main(["retrieval"]) == cli.EXIT_SETUP
    assert "MAKNA_DATABASE_URL" in capsys.readouterr().err


def test_a_broken_gates_file_is_a_setup_problem_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gates = tmp_path / "gates.toml"
    gates.write_text("[retrieval]\nmrr_min = 0.75\n")
    assert cli.main(["all", "--gates", str(gates)]) == cli.EXIT_SETUP
    assert "missing" in capsys.readouterr().err


def test_an_unreachable_database_is_exit_2_with_the_class_name_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MAKNA_DATABASE_URL", "postgresql://u:secretpw@127.0.0.1:1/nodb")
    monkeypatch.setenv("MAKNA_EMBEDDING_PROVIDER", "hash")
    assert cli.main(["retrieval", "--out", "/nonexistent-should-not-be-written"]) == cli.EXIT_SETUP
    err = capsys.readouterr().err
    assert "OperationalError" in err and "Traceback" not in err
    assert "secretpw" not in err and "127.0.0.1" not in err


def test_engine_text_reaches_standard_error_only_with_verbose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.log_for(False)("engine says: personal value")
    assert capsys.readouterr() == ("", "")
    cli.log_for(True)("engine says: personal value")
    captured = capsys.readouterr()
    assert captured.out == "" and "personal value" in captured.err
