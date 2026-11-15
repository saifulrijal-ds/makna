"""WP-12B acceptance (integration tier), on a scratch PostgreSQL:

1. A token is shown once at creation, stored only as a hash, rejected after revocation, and
   `last_used_at` is updated.
2. A token's `Caller` has at most the viewer permissions of its user, even for an admin.
3. Passwords are stored as Argon2 hashes, and the demo-user `init` step is idempotent.

Plus: one error for every failed authentication, users and tokens are never deleted (so a call
whose token is revoked meanwhile still writes its log row), and the commands.
"""

from __future__ import annotations

import io
import time
from typing import Any

import pytest
from argon2 import PasswordHasher
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from deploy.init import DEMO_USERS, Context, create_demo_users
from makna.cli.main import main
from makna.index.tables import api_token, app_user, tool_call_log
from makna.services import Caller, accounts
from makna.services import permissions as p
from makna.services.accounts import AccountError, AuthenticationError

from . import service_support as sup

PASSWORD = "a long enough password"
SAME_MESSAGE = "authentication failed"


def make_user(
    engine: Engine, email: str = "ann@demo.example", role: str = "viewer", **more: Any
) -> accounts.User:
    return accounts.create_user(
        engine, email, more.get("name", "Ann"), role, more.get("teams", []), PASSWORD
    )


def row_of(engine: Engine, table: Any, **where: Any) -> dict[str, Any]:
    statement = select(table)
    for column, value in where.items():
        statement = statement.where(table.c[column] == value)
    with engine.connect() as connection:
        return dict(connection.execute(statement).mappings().one())


def all_rows(engine: Engine, table: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [tuple(r) for r in connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))]


# --- acceptance 1: tokens ---


def test_a_token_is_shown_once_stored_only_as_a_hash_and_rejected_after_revocation(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    user = make_user(engine, role="admin")
    new = accounts.create_token(engine, "ann@demo.example", "agent")
    assert new.token.startswith("mkn_") and new.name == "agent"

    stored = row_of(engine, api_token, id=new.id)
    assert stored["token_hash"] == accounts.hash_token(new.token)
    assert stored["user_id"] == user.id
    # The clear token is in no column of the row and in no listing.
    assert all(new.token not in str(value) for value in stored.values())
    assert all(new.token not in repr(t) for t in accounts.list_tokens(engine))
    with engine.connect() as connection:
        dump = connection.execute(text("SELECT row_to_json(t)::text FROM api_token t")).scalars()
        assert all(new.token not in line for line in dump)

    assert stored["last_used_at"] is None
    caller = accounts.authenticate_token(engine, new.token, "mcp")
    assert caller.token_id == new.id and caller.user_id == user.id
    first = row_of(engine, api_token, id=new.id)["last_used_at"]
    assert first is not None
    time.sleep(0.02)
    accounts.authenticate_token(engine, new.token, "mcp")
    assert row_of(engine, api_token, id=new.id)["last_used_at"] > first

    accounts.revoke_token(engine, new.id)
    with pytest.raises(AuthenticationError):
        accounts.authenticate_token(engine, new.token, "mcp")
    revoked = row_of(engine, api_token, id=new.id)
    assert revoked["revoked_at"] is not None
    assert revoked["last_used_at"] > first  # the refused attempt did not touch it


def test_revoking_twice_keeps_the_first_time_and_an_unknown_id_is_an_error(
    migrated_engine: Engine,
) -> None:
    make_user(migrated_engine)
    new = accounts.create_token(migrated_engine, "ann@demo.example", "t")
    accounts.revoke_token(migrated_engine, new.id)
    first = row_of(migrated_engine, api_token, id=new.id)["revoked_at"]
    time.sleep(0.02)
    accounts.revoke_token(migrated_engine, new.id)
    assert row_of(migrated_engine, api_token, id=new.id)["revoked_at"] == first
    with pytest.raises(AccountError):
        accounts.revoke_token(migrated_engine, new.id + 1000)


def test_every_failure_raises_the_same_error_and_leaves_last_used_alone(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    make_user(engine)
    make_user(engine, "bob@demo.example")
    revoked = accounts.create_token(engine, "ann@demo.example", "revoked")
    accounts.revoke_token(engine, revoked.id)
    of_inactive = accounts.create_token(engine, "bob@demo.example", "of inactive")
    accounts.deactivate_user(engine, "bob@demo.example")
    unknown = accounts.generate_token()
    cases = {
        "unknown": unknown,
        "revoked": revoked.token,
        "inactive user": of_inactive.token,
        "wrong prefix": "xyz_" + unknown[4:],
        "too short": unknown[:-1],
        "outside the alphabet": unknown[:-1] + "!",
        "surrounding whitespace": " " + unknown,
        "empty": "",
    }
    seen = []
    for name, raw in cases.items():
        with pytest.raises(AuthenticationError) as raised:
            accounts.authenticate_token(engine, raw, "mcp")
        seen.append((type(raised.value), str(raised.value), raised.value.args))
        assert raised.value.__cause__ is None, name
    assert len(set(seen)) == 1 and seen[0][1] == SAME_MESSAGE
    assert all(t["last_used_at"] is None for t in all_token_rows(engine))


def all_token_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(select(api_token)).mappings()]


def test_a_deactivated_user_stops_signing_in_and_all_tokens_at_once(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    user = make_user(engine)
    token = accounts.create_token(engine, "ann@demo.example", "t")
    assert accounts.verify_password(engine, "ann@demo.example", PASSWORD) is not None
    accounts.authenticate_token(engine, token.token, "mcp")
    accounts.caller_for_user(engine, user.id, "web")

    accounts.deactivate_user(engine, "ann@demo.example")
    accounts.deactivate_user(engine, "ann@demo.example")  # twice changes nothing
    assert accounts.verify_password(engine, "ann@demo.example", PASSWORD) is None
    with pytest.raises(AuthenticationError):
        accounts.authenticate_token(engine, token.token, "mcp")
    with pytest.raises(AuthenticationError):
        accounts.caller_for_user(engine, user.id, "web")
    with pytest.raises(AccountError):
        accounts.create_token(engine, "ann@demo.example", "another")
    with pytest.raises(AccountError):
        accounts.deactivate_user(engine, "nobody@demo.example")


# --- acceptance 2: a token never acts above viewer ---


def test_the_caller_of_an_admin_token_has_viewer_permissions_only(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    admin = make_user(engine, "root@demo.example", "admin", teams=["Ops"])
    token = accounts.create_token(engine, "root@demo.example", "agent")

    caller = accounts.authenticate_token(engine, token.token, "mcp")
    assert caller == Caller(
        user_id=admin.id, role="admin", channel="mcp", teams=("ops",), token_id=token.id
    )
    assert caller.effective_role == "viewer"

    assert p.can_read_approved(caller) and p.can_compile(caller)
    assert not p.can_see_drafts(caller)
    assert not p.can_edit_proposals(caller)
    assert p.approval_grant(caller) is p.Grant.NO
    assert not p.can_see_audit_log(caller)
    assert not p.can_see_tool_call_log(caller)
    assert not p.can_manage(caller)
    assert not p.can_use_test_bench(caller)
    assert p.column_of(caller) == p.AGENT_TOKEN
    for permission in p.Permission:
        row = p.MATRIX[permission]
        assert p.grant(caller, permission) is row[p.AGENT_TOKEN]
        if row[p.AGENT_TOKEN] is p.Grant.YES:  # never more than a viewer has
            assert row["viewer"] is p.Grant.YES
    # The same user, signed in, keeps the admin role.
    signed_in = accounts.caller_for_user(engine, admin.id, "web")
    assert signed_in.token_id is None and signed_in.effective_role == "admin"
    assert p.can_manage(signed_in)


def test_the_channel_is_the_one_given(migrated_engine: Engine) -> None:
    make_user(migrated_engine)
    token = accounts.create_token(migrated_engine, "ann@demo.example", "t")
    assert accounts.authenticate_token(migrated_engine, token.token, "cli").channel == "cli"


# --- acceptance 3: passwords ---


def test_passwords_are_stored_as_argon2id_hashes_and_never_listed(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    user = make_user(engine, " Ann@Demo.Example ", teams=[" Credit-Risk "])
    stored = row_of(engine, app_user, id=user.id)
    assert stored["password_hash"].startswith("$argon2id$")
    assert PASSWORD not in str(stored)
    assert PasswordHasher().verify(stored["password_hash"], PASSWORD)
    assert stored["email"] == "ann@demo.example" and stored["teams"] == ["credit-risk"]
    assert stored["is_active"] is True

    listed = accounts.list_users(engine)
    assert [u.email for u in listed] == ["ann@demo.example"]
    assert "hash" not in " ".join(vars(listed[0]).keys()).lower()
    assert stored["password_hash"] not in repr(listed)

    with pytest.raises(AccountError) as raised:
        make_user(engine, "ANN@demo.example")
    assert "already exists" in str(raised.value)
    assert stored["password_hash"] not in str(raised.value)
    assert len(accounts.list_users(engine)) == 1


def test_a_short_password_stores_nothing(migrated_engine: Engine) -> None:
    with pytest.raises(AccountError):
        accounts.create_user(migrated_engine, "a@demo.example", "A", "viewer", [], "short")
    assert accounts.list_users(migrated_engine) == []


def test_verify_password_answers_none_for_unknown_wrong_and_inactive_alike(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = migrated_engine
    make_user(engine)
    make_user(engine, "off@demo.example")
    accounts.deactivate_user(engine, "off@demo.example")

    calls: list[str] = []
    real = accounts._hasher

    class Counting:
        def verify(self, hash_: str, password: str) -> bool:
            calls.append(hash_)
            return real.verify(hash_, password)

        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

    monkeypatch.setattr(accounts, "_hasher", Counting())
    ok = accounts.verify_password(engine, " ANN@demo.example", PASSWORD)
    assert ok is not None and ok.email == "ann@demo.example" and ok.role == "viewer"
    assert ok.caller("web").user_id == ok.id
    results = [
        accounts.verify_password(engine, "ann@demo.example", "the wrong password"),
        accounts.verify_password(engine, "nobody@demo.example", PASSWORD),
        accounts.verify_password(engine, "off@demo.example", PASSWORD),
        accounts.verify_password(engine, "not an email", PASSWORD),
    ]
    assert results == [None, None, None, None]
    # One hash verification in every case, an unknown email included.
    assert len(calls) == 5
    assert calls[2].startswith("$argon2id$")


def test_a_hash_made_with_older_parameters_is_replaced_on_sign_in(
    migrated_engine: Engine,
) -> None:
    engine = migrated_engine
    user = make_user(engine)
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    with engine.begin() as connection:
        connection.execute(text("UPDATE app_user SET password_hash = :h"), {"h": weak})
    assert accounts.verify_password(engine, "ann@demo.example", "the wrong password") is None
    assert row_of(engine, app_user, id=user.id)["password_hash"] == weak
    assert accounts.verify_password(engine, "ann@demo.example", PASSWORD) is not None
    renewed = row_of(engine, app_user, id=user.id)["password_hash"]
    assert renewed != weak and not PasswordHasher().check_needs_rehash(renewed)
    assert accounts.verify_password(engine, "ann@demo.example", PASSWORD) is not None


def test_a_stored_value_that_is_not_a_hash_never_signs_in(migrated_engine: Engine) -> None:
    sup.add_users(migrated_engine)  # the service tests store "not-a-real-hash"
    assert accounts.verify_password(migrated_engine, "admin@example.com", "not-a-real-hash") is None


# --- acceptance 3: the demo users ---


def demo_context(url: URL, password: str | None) -> Context:
    from pathlib import Path

    plain = url.render_as_string(hide_password=False)
    return Context(
        data_dir=Path("unused"),
        seed=1,
        scale=1.0,
        database_url=plain,
        lending_loader_url=plain,
        demo_password=password,
    )


def test_the_demo_step_creates_four_users_and_a_second_run_changes_nothing(
    migrated_url: URL, migrated_engine: Engine
) -> None:
    context = demo_context(migrated_url, "demo password 123")
    assert create_demo_users(context) == "created viewer, curator, approver, admin"
    first = all_rows(migrated_engine, "app_user")
    assert len(first) == 4
    with migrated_engine.connect() as connection:
        connection.execute(text("SELECT pg_stat_clear_snapshot()"))
    assert create_demo_users(context) == "unchanged"
    assert all_rows(migrated_engine, "app_user") == first  # hashes and ids included

    by_email = {u.email: u for u in accounts.list_users(migrated_engine)}
    assert {e: (u.role, u.teams) for e, u in by_email.items()} == {
        "viewer@demo.example": ("viewer", ("credit-risk",)),
        "curator@demo.example": ("curator", ("credit-risk",)),
        "approver@demo.example": ("approver", ("data-governance",)),
        "admin@demo.example": ("admin", ()),
    }
    assert [d.email for d in DEMO_USERS] == list(by_email)
    for email in by_email:
        assert accounts.verify_password(migrated_engine, email, "demo password 123") is not None
        stored = row_of(migrated_engine, app_user, email=email)
        assert stored["password_hash"].startswith("$argon2id$")


def test_the_demo_step_without_a_password_creates_nothing_and_succeeds(
    migrated_url: URL, migrated_engine: Engine
) -> None:
    for missing in (None, ""):
        outcome = create_demo_users(demo_context(migrated_url, missing))
        assert outcome.startswith("skipped (MAKNA_DEMO_PASSWORD is not set")
    assert all_rows(migrated_engine, "app_user") == []


def test_the_demo_step_leaves_an_existing_user_exactly_as_it_is(
    migrated_url: URL, migrated_engine: Engine
) -> None:
    accounts.create_user(
        migrated_engine, "viewer@demo.example", "Someone Else", "admin", ["x"], PASSWORD
    )
    before = row_of(migrated_engine, app_user, email="viewer@demo.example")
    outcome = create_demo_users(demo_context(migrated_url, "demo password 123"))
    assert outcome == "created curator, approver, admin"
    assert row_of(migrated_engine, app_user, email="viewer@demo.example") == before
    assert accounts.verify_password(migrated_engine, "viewer@demo.example", PASSWORD) is not None


def test_a_short_demo_password_fails_the_step_without_printing_it(
    migrated_url: URL, migrated_engine: Engine
) -> None:
    with pytest.raises(AccountError) as raised:
        create_demo_users(demo_context(migrated_url, "short"))
    assert "short" not in str(raised.value).replace("at least", "")
    assert all_rows(migrated_engine, "app_user") == []


def test_the_whole_init_list_prints_no_password(migrated_url: URL, migrated_engine: Engine) -> None:
    from deploy.init import STEPS, Step, run_steps

    context = demo_context(migrated_url, "demo password 123")
    out = io.StringIO()
    demo_only = [s for s in STEPS if s.name == "demo-users"]
    assert len(demo_only) == 1 and isinstance(demo_only[0], Step)
    assert run_steps(demo_only, context, out) == 0
    assert "demo password 123" not in out.getvalue() + repr(context)


# --- users and tokens are never deleted ---


def test_a_call_made_with_a_token_revoked_meanwhile_still_writes_its_log_row(
    fresh_service_world: tuple[Engine, sup.Users],
) -> None:
    engine, _ = fresh_service_world
    make_user(engine, "agent@demo.example", "admin")
    token = accounts.create_token(engine, "agent@demo.example", "agent")
    caller = accounts.authenticate_token(engine, token.token, "mcp")
    svc = sup.services(engine)

    accounts.revoke_token(engine, token.id)  # after authentication, before the call
    accounts.deactivate_user(engine, "agent@demo.example")
    before = sup.log_count(engine)
    assert svc.search(caller, "kredit macet").hits
    assert sup.log_count(engine) == before + 1
    row = sup.last_log(engine)
    assert row["token_id"] == token.id and row["user_id"] == caller.user_id
    assert row["status"] == "ok"
    # The token row is still there, revoked, so the foreign key holds.
    assert row_of(engine, api_token, id=token.id)["revoked_at"] is not None
    # And a new call with the same token is refused at authentication.
    with pytest.raises(AuthenticationError):
        accounts.authenticate_token(engine, token.token, "mcp")


def test_there_is_no_way_to_delete_a_user_or_a_token(
    fresh_service_world: tuple[Engine, sup.Users],
) -> None:
    engine, _ = fresh_service_world
    make_user(engine, "agent@demo.example")
    token = accounts.create_token(engine, "agent@demo.example", "agent")
    caller = accounts.authenticate_token(engine, token.token, "mcp")
    sup.services(engine).search(caller, "kredit macet")

    public = [name for name in dir(accounts) if not name.startswith("_")]
    assert not [name for name in public if "delete" in name.lower() or "remove" in name.lower()]
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(delete(api_token).where(api_token.c.id == token.id))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(delete(app_user).where(app_user.c.id == caller.user_id))
    with engine.connect() as connection:
        assert connection.execute(select(tool_call_log.c.token_id)).scalars().all()


def test_the_commands_offer_no_delete(capsys: pytest.CaptureFixture[str]) -> None:
    for group in ("user", "token"):
        with pytest.raises(SystemExit):
            main([group, "delete"])
        assert "invalid choice" in capsys.readouterr().err


# --- a signed-in user ---


def test_a_signed_in_user_becomes_a_caller_with_no_token(migrated_engine: Engine) -> None:
    user = make_user(migrated_engine, "cur@demo.example", "curator", teams=["credit-risk"])
    caller = accounts.caller_for_user(migrated_engine, user.id, "web")
    assert caller == Caller(user_id=user.id, role="curator", channel="web", teams=("credit-risk",))
    assert caller.token_id is None
    with pytest.raises(AuthenticationError) as raised:
        accounts.caller_for_user(migrated_engine, user.id + 99, "web")
    assert str(raised.value) == SAME_MESSAGE


# --- listings ---


def test_listings_never_hold_a_hash_and_filter_by_user(migrated_engine: Engine) -> None:
    engine = migrated_engine
    make_user(engine)
    make_user(engine, "bob@demo.example")
    a = accounts.create_token(engine, "ann@demo.example", "a")
    b = accounts.create_token(engine, "bob@demo.example", "b")
    accounts.revoke_token(engine, b.id)
    accounts.authenticate_token(engine, a.token, "mcp")

    everything = accounts.list_tokens(engine)
    assert [(t.id, t.name, t.user_email) for t in everything] == [
        (a.id, "a", "ann@demo.example"),
        (b.id, "b", "bob@demo.example"),
    ]
    assert everything[0].last_used_at is not None and everything[0].revoked_at is None
    assert everything[1].revoked_at is not None
    assert [t.id for t in accounts.list_tokens(engine, " BOB@demo.example")] == [b.id]
    shown = repr(everything) + repr(accounts.list_users(engine))
    for secret in (a.token, b.token, accounts.hash_token(a.token), "$argon2id$"):
        assert secret not in shown


# --- the commands ---


@pytest.fixture
def cli_database(migrated_url: URL, monkeypatch: pytest.MonkeyPatch) -> URL:
    monkeypatch.setenv("MAKNA_DATABASE_URL", migrated_url.render_as_string(hide_password=False))
    return migrated_url


def run_cli(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    *argv: str,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_commands_create_list_revoke_and_deactivate(
    cli_database: URL, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out, err = run_cli(
        capsys,
        monkeypatch,
        *["user", "create", "--email", "Dee@Demo.Example", "--name", "Dee", "--role", "curator"],
        *["--team", "Credit-Risk", "--password-stdin"],
        stdin=PASSWORD + "\n",
    )
    assert code == 0 and "dee@demo.example" in out and PASSWORD not in out + err

    code, out, err = run_cli(capsys, monkeypatch, "user", "list")
    assert code == 0 and "dee@demo.example  curator  teams: credit-risk  active" in out
    assert "$argon2" not in out + err

    code, out, err = run_cli(
        capsys, monkeypatch, "token", "create", "--user", "dee@demo.example", "--name", "bot"
    )
    assert code == 0
    token_lines = [line for line in out.splitlines() if line.startswith("token: mkn_")]
    assert len(token_lines) == 1
    token = token_lines[0].removeprefix("token: ")
    assert "will not be shown again" in out and accounts.is_well_formed_token(token)

    code, out, err = run_cli(capsys, monkeypatch, "token", "list")
    assert code == 0 and "bot  dee@demo.example" in out and "revoked -" in out
    assert token not in out + err and accounts.hash_token(token) not in out + err
    engine = accounts_engine(cli_database)
    try:
        assert accounts.authenticate_token(engine, token, "cli").effective_role == "viewer"
        token_id = int(out.split()[0])
        code, out, err = run_cli(capsys, monkeypatch, "token", "revoke", "--id", str(token_id))
        assert code == 0 and "revoked token" in out
        with pytest.raises(AuthenticationError):
            accounts.authenticate_token(engine, token, "cli")
        code, out, _ = run_cli(capsys, monkeypatch, "token", "list", "--user", "dee@demo.example")
        assert code == 0 and "revoked -" not in out

        code, out, err = run_cli(
            capsys, monkeypatch, "user", "deactivate", "--email", "dee@demo.example"
        )
        assert code == 0
        assert accounts.verify_password(engine, "dee@demo.example", PASSWORD) is None
        code, out, _ = run_cli(capsys, monkeypatch, "user", "list")
        assert "inactive" in out
    finally:
        engine.dispose()


def accounts_engine(url: URL) -> Engine:
    from sqlalchemy import create_engine

    return create_engine(url)


def test_the_commands_fail_with_one_line_and_no_traceback(
    cli_database: URL, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    create = ["user", "create", "--email", "eve@demo.example", "--name", "Eve", "--role", "viewer"]
    assert run_cli(capsys, monkeypatch, *create, "--password-stdin", stdin=PASSWORD)[0] == 0
    for argv, stdin in (
        ([*create, "--password-stdin"], PASSWORD),  # duplicate
        ([*create[:3], "not-an-email", *create[4:], "--password-stdin"], PASSWORD),
        ([*create, "--password-stdin"], ""),  # no password on standard input
        (["token", "create", "--user", "ghost@demo.example", "--name", "x"], None),
        (["token", "revoke", "--id", "9999"], None),
        (["user", "deactivate", "--email", "ghost@demo.example"], None),
    ):
        code, out, err = run_cli(capsys, monkeypatch, *argv, stdin=stdin)
        assert code == 1, argv
        assert len(err.strip().splitlines()) == 1 and "Traceback" not in err, err
        assert PASSWORD not in out + err and "$argon2" not in out + err
        assert err.startswith("makna ")
