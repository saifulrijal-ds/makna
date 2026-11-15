"""WP-12B (unit tier): token format and hashing, the malformed-token checks, password length,
email and team normalisation, and the CLI's refusal to take a password as an argument. No
database is used: an engine that fails on any use proves a check runs before the database."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine

from makna.cli.main import main
from makna.services import accounts
from makna.services.accounts import AccountError, AuthenticationError, NewToken

ROOT = Path(__file__).resolve().parents[2]


def untouchable_engine() -> Engine:
    engine = MagicMock(spec=Engine)
    engine.begin.side_effect = AssertionError("the database was used")
    engine.connect.side_effect = AssertionError("the database was used")
    return engine  # type: ignore[no-any-return]


# --- tokens ---


def test_a_token_is_the_prefix_and_32_random_bytes_in_url_safe_base64() -> None:
    token = accounts.generate_token()
    assert token.startswith("mkn_") and len(token) == 4 + 43
    assert re.fullmatch(r"mkn_[A-Za-z0-9_-]{43}", token)
    assert accounts.is_well_formed_token(token)
    assert accounts.generate_token() != token


def test_the_stored_hash_is_the_lower_case_hex_sha256_of_the_whole_token() -> None:
    token = accounts.generate_token()
    expected = hashlib.sha256(token.encode()).hexdigest()
    assert accounts.hash_token(token) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert token not in accounts.hash_token(token)


GOOD = "mkn_" + "A" * 43
MALFORMED = [
    "",
    "mkn_",
    "A" * 47,
    "mkn_" + "A" * 42,
    "mkn_" + "A" * 44,
    "xyz_" + "A" * 43,
    "MKN_" + "A" * 43,
    "mkn_" + "A" * 42 + "=",
    "mkn_" + "A" * 42 + "+",
    "mkn_" + "A" * 42 + "/",
    "mkn_" + "A" * 42 + " ",
    " " + GOOD,
    GOOD + " ",
    GOOD + "\n",
    "Bearer " + GOOD,
    "mkn_" + "A" * 42 + "é",
]


def test_the_good_shape_is_accepted_and_every_malformed_one_refused() -> None:
    assert accounts.is_well_formed_token(GOOD)
    for raw in MALFORMED:
        assert not accounts.is_well_formed_token(raw), repr(raw)
    assert not accounts.is_well_formed_token(None)
    assert not accounts.is_well_formed_token(b"mkn_" + b"A" * 43)


def test_a_malformed_token_is_refused_before_the_database_is_read() -> None:
    engine = untouchable_engine()
    messages = set()
    for raw in MALFORMED:
        with pytest.raises(AuthenticationError) as raised:
            accounts.authenticate_token(engine, raw, "mcp")
        messages.add(str(raised.value))
    assert messages == {"authentication failed"}


def test_the_authentication_error_has_one_fixed_message() -> None:
    assert str(AuthenticationError()) == "authentication failed"
    with pytest.raises(TypeError):
        AuthenticationError("revoked")  # type: ignore[call-arg]


def test_an_unknown_channel_is_a_programming_error_not_an_authentication_failure() -> None:
    with pytest.raises(ValueError):
        accounts.authenticate_token(untouchable_engine(), GOOD, "carrier-pigeon")


def test_a_new_token_does_not_show_itself_in_its_repr() -> None:
    new = NewToken(id=1, name="n", token=GOOD)
    assert GOOD not in repr(new) and GOOD not in str(new)


# --- passwords ---


def test_a_password_needs_12_characters_and_the_error_does_not_repeat_it() -> None:
    short = "elevenchars"
    assert len(short) == 11
    with pytest.raises(AccountError) as raised:
        accounts.hash_password(short)
    assert short not in str(raised.value) and "12" in str(raised.value)
    assert accounts.hash_password("twelve-chars").startswith("$argon2id$")


def test_a_password_is_counted_in_characters_not_bytes() -> None:
    assert accounts.hash_password("é" * 12).startswith("$argon2id$")
    with pytest.raises(AccountError):
        accounts.hash_password("é" * 11)


def test_hashes_differ_per_call_and_verify_with_the_library() -> None:
    first = accounts.hash_password("correct horse battery")
    second = accounts.hash_password("correct horse battery")
    assert first != second
    assert accounts._hasher.verify(first, "correct horse battery")


# --- emails, teams, other fields ---


def test_an_email_is_trimmed_and_lower_cased() -> None:
    assert accounts.normalize_email("  Jane.Doe@Demo.Example ") == "jane.doe@demo.example"


@pytest.mark.parametrize(
    "bad", ["", "   ", "no-at-sign", "a@", "@b", "a b@c", "a@b@c", "x" * 260 + "@y.z"]
)
def test_a_bad_email_is_refused(bad: str) -> None:
    with pytest.raises(AccountError):
        accounts.normalize_email(bad)


def test_teams_are_trimmed_lower_case_non_empty_and_unique() -> None:
    assert accounts.normalize_teams([" Credit-Risk ", "DATA-governance", "credit-risk"]) == (
        "credit-risk",
        "data-governance",
    )
    assert accounts.normalize_teams([]) == ()
    for bad in ([""], ["  "], ["ok", ""], "credit-risk", [1]):
        with pytest.raises(AccountError):
            accounts.normalize_teams(bad)  # type: ignore[arg-type]


def test_create_user_validates_before_it_touches_the_database() -> None:
    engine = untouchable_engine()
    good = ("a@demo.example", "A", "viewer", [], "long enough password")
    for index, bad in ((0, "nope"), (1, " "), (2, "root"), (3, [""]), (4, "short")):
        args = list(good)
        args[index] = bad
        with pytest.raises(AccountError):
            accounts.create_user(engine, *args)  # type: ignore[arg-type]


# --- the command line ---


def test_the_cli_refuses_a_password_argument_without_echoing_it(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    secret = "do-not-echo-this-1234"
    for form in (["--password", secret], [f"--password={secret}"]):
        code = main(
            [
                "user",
                "create",
                "--email",
                "a@demo.example",
                "--name",
                "A",
                "--role",
                "viewer",
                *form,
            ]
        )
        captured = capsys.readouterr()
        assert code == 2
        assert secret not in captured.out + captured.err
        assert "cannot be given as an argument" in captured.err


def test_the_cli_help_offers_no_password_argument_and_names_the_operator_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["user", "create", "--help"])
    create_help = capsys.readouterr().out
    assert "--password-stdin" in create_help
    assert not re.search(r"--password\b(?!-)", create_help.replace("--password-stdin", ""))
    with pytest.raises(SystemExit):
        main(["user", "--help"])
    assert "operator" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["token", "--help"])
    assert "operator" in capsys.readouterr().out


def test_the_cli_needs_a_database_setting(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    for argv in (["user", "list"], ["token", "list"], ["token", "revoke", "--id", "1"]):
        assert main(argv) == 2
        captured = capsys.readouterr()
        assert "MAKNA_DATABASE_URL" in captured.err and "Traceback" not in captured.err


def test_the_cli_reads_the_password_from_standard_input_once_and_checks_the_length(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("tooshort\n"))
    code = main(
        [
            "user",
            "create",
            "--email",
            "a@demo.example",
            "--name",
            "A",
            "--role",
            "viewer",
            "--password-stdin",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1  # refused for length before any database is needed
    assert "tooshort" not in captured.out + captured.err
    assert "at least 12 characters" in captured.err


def test_the_cli_asks_twice_and_stops_when_the_entries_differ(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter(["first password 1", "second password 2"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    code = main(["user", "create", "--email", "a@demo.example", "--name", "A", "--role", "viewer"])
    captured = capsys.readouterr()
    assert code == 1 and "differ" in captured.err
    assert "password 1" not in captured.out + captured.err


# --- no password value in any committed file ---

DEMO_VALUE = re.compile(r"MAKNA_DEMO_PASSWORD[\"']?[ \t]*[:=][ \t]*(?P<value>\S*)")
ALLOWED_VALUES = {"", "${MAKNA_DEMO_PASSWORD:-}", "..."}


def committed_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    return [ROOT / name for name in listed if name]


def demo_password_values(text: str) -> list[str]:
    found = []
    for match in DEMO_VALUE.finditer(text):
        value = match.group("value")
        # A value taken from the shell (`$pw`, `$(openssl ...)`) or the placeholder is no secret.
        if value not in ALLOWED_VALUES and not value.startswith("$"):
            found.append(value)
    return found


def test_no_committed_file_holds_a_value_for_the_demo_password() -> None:
    this_file = Path(__file__).resolve()
    scanned = 0
    for path in committed_files():
        if path.resolve() == this_file or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        assert demo_password_values(text) == [], (
            f"{path.relative_to(ROOT)} sets MAKNA_DEMO_PASSWORD"
        )
    assert scanned > 50


def test_the_scan_detects_a_value() -> None:
    assert demo_password_values("MAKNA_DEMO_PASSWORD=hunter2-hunter2") == ["hunter2-hunter2"]
    assert demo_password_values("  MAKNA_DEMO_PASSWORD: secret-secret-1") == ["secret-secret-1"]
    assert demo_password_values("MAKNA_DEMO_PASSWORD=\nMAKNA_X=1") == []
    assert demo_password_values("MAKNA_DEMO_PASSWORD: ${MAKNA_DEMO_PASSWORD:-}") == []
    assert demo_password_values('echo "MAKNA_DEMO_PASSWORD=$pw" >> f') == []
    assert demo_password_values("export MAKNA_DEMO_PASSWORD=$(openssl rand -hex 16)") == []
    assert demo_password_values("export MAKNA_DEMO_PASSWORD=...") == []
