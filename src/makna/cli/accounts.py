"""The `makna user` and `makna token` commands.

These are the administrator's path: the command holds `MAKNA_DATABASE_URL`, so whoever can run
it can already change the database. They act as the operator and build no `Caller`. A password
is read with `getpass` or from standard input, never from an argument, so it does not land in
shell history or the process list. A token is printed once, when it is created.
"""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable, Sequence

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from makna.index.db import database_url_from_env, make_engine
from makna.services import accounts
from makna.services.accounts import AccountError

PASSWORD_ARGUMENT_REFUSED = (
    "a password cannot be given as an argument (it would stay in the shell history and the "
    "process list); omit it to be asked, or use --password-stdin"
)


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        password = line.removesuffix("\n").removesuffix("\r")
        if not password:
            raise AccountError("password: standard input had no password on its first line")
        return password
    first = getpass.getpass("Password: ")
    if getpass.getpass("Password again: ") != first:
        raise AccountError("password: the two entries differ")
    return first


def _run(command: str, work: Callable[[Engine], int]) -> int:
    """Run `work(engine)` and return its exit code, with one-line errors and no traceback."""
    try:
        engine = make_engine(database_url_from_env())
    except RuntimeError as error:
        print(f"makna {command}: {error}", file=sys.stderr)
        return 2
    try:
        code = work(engine)
    except AccountError as error:
        print(f"makna {command}: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        # Not str(error): the text of a database error carries the SQL parameters.
        print(
            f"makna {command}: database error ({type(error).__name__}); "
            "check MAKNA_DATABASE_URL and that the migrations have run",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()
    return code


def user_create(
    email: str, name: str, role: str, teams: Sequence[str], password_stdin: bool, refused: bool
) -> int:
    if refused:
        print(f"makna user create: {PASSWORD_ARGUMENT_REFUSED}", file=sys.stderr)
        return 2
    try:
        password = _read_password(password_stdin)
        accounts.check_password_policy(password)
    except (AccountError, EOFError) as error:
        print(f"makna user create: {error}", file=sys.stderr)
        return 1

    def work(engine: Engine) -> int:
        user = accounts.create_user(engine, email, name, role, teams, password)
        print(f"created user {user.id}: {user.email} ({user.role})")
        return 0

    return _run("user create", work)


def user_list() -> int:
    def work(engine: Engine) -> int:
        for user in accounts.list_users(engine):
            state = "active" if user.is_active else "inactive"
            print(
                f"{user.id}  {user.email}  {user.role}  teams: {', '.join(user.teams) or '-'}  "
                f"{state}"
            )
        return 0

    return _run("user list", work)


def user_deactivate(email: str) -> int:
    def work(engine: Engine) -> int:
        user = accounts.deactivate_user(engine, email)
        print(f"deactivated {user.email}: sign-in and all tokens of this user stop working")
        return 0

    return _run("user deactivate", work)


def token_create(user: str, name: str) -> int:
    def work(engine: Engine) -> int:
        created = accounts.create_token(engine, user, name)
        print(f"created token {created.id} ({created.name}) for {user.strip().lower()}")
        print(f"token: {created.token}")
        print("This token will not be shown again. Store it now; it acts as a viewer only.")
        return 0

    return _run("token create", work)


def token_list(user: str | None) -> int:
    def work(engine: Engine) -> int:
        for token in accounts.list_tokens(engine, user):

            def when(value: object) -> str:
                return str(value) if value is not None else "-"

            print(
                f"{token.id}  {token.name}  {token.user_email}  created {token.created_at}  "
                f"last used {when(token.last_used_at)}  revoked {when(token.revoked_at)}"
            )
        return 0

    return _run("token list", work)


def token_revoke(token_id: int) -> int:
    def work(engine: Engine) -> int:
        accounts.revoke_token(engine, token_id)
        print(f"revoked token {token_id}")
        return 0

    return _run("token revoke", work)
