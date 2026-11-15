"""Users and API tokens (ARCHITECTURE section 11, WEB-5).

This module stores users and tokens and turns a bearer token or a signed-in user into a
`Caller`. It builds no sign-in page, session, or HTTP middleware: WP-16 and WP-13 use the
functions here.

- Passwords are hashed with Argon2id (library defaults) and the full encoded hash is stored.
  A password is never logged, printed, or put in an exception message.
- A token is `mkn_` plus 32 random bytes, URL-safe base64 without padding. Only the SHA-256
  hex digest of the whole string is stored. The clear token is returned once, by `create_token`.
- `authenticate_token` raises one `AuthenticationError`, with one fixed message, for every kind
  of failure, so a caller cannot tell an unknown token from a revoked one.
- Users and tokens are never deleted: `tool_call_log` and `audit_event` refer to them. A user is
  deactivated and a token is revoked; both stop working at once.

Database errors can carry the SQL parameters (a password hash, a token hash), so callers of
these functions must not print the text of a `SQLAlchemyError` (see
`docs/notes/database-error-text-carries-the-query-parameters.md`).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Engine, RowMapping, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from makna.index.tables import api_token, app_user
from makna.services.caller import CHANNELS, ROLES, Caller

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "mkn_"
TOKEN_BYTES = 32
_TOKEN_BODY_LENGTH = 43  # 32 bytes in URL-safe base64 without padding
_TOKEN_PATTERN = re.compile(rf"{re.escape(TOKEN_PREFIX)}[A-Za-z0-9_-]{{{_TOKEN_BODY_LENGTH}}}")
MIN_PASSWORD_LENGTH = 12
MAX_NAME_LENGTH = 200
_EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+")
MAX_EMAIL_LENGTH = 254
_FAILURE_MESSAGE = "authentication failed"


class AccountError(ValueError):
    """Input that cannot be used (a bad email, a short password, a duplicate, an unknown user).
    The message never holds a password or a token."""


class AuthenticationError(Exception):
    """A token or user that cannot act. There is one message for every cause."""

    def __init__(self) -> None:
        super().__init__(_FAILURE_MESSAGE)


@dataclass(frozen=True)
class User:
    """A user as the services see it. It never holds the password hash."""

    id: int
    email: str
    display_name: str
    role: str
    teams: tuple[str, ...]
    is_active: bool
    created_at: datetime

    def caller(self, channel: str) -> Caller:
        return Caller(user_id=self.id, role=self.role, channel=channel, teams=self.teams)


@dataclass(frozen=True)
class NewToken:
    """A token just created. `token` is the only copy of the clear value, so it is left out of
    `repr` to keep it out of logs and tracebacks."""

    id: int
    name: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class TokenInfo:
    """A token as listed: never the token and never its hash."""

    id: int
    name: str
    user_email: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


# --- validation ------------------------------------------------------------------------------


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise AccountError("email: must be text")
    value = email.strip().lower()
    if not value or len(value) > MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.fullmatch(value):
        raise AccountError("email: must look like name@domain")
    return value


def normalize_teams(teams: Iterable[str]) -> tuple[str, ...]:
    if isinstance(teams, str):
        raise AccountError("teams: give a list of team names")
    result: list[str] = []
    for team in teams:
        if not isinstance(team, str) or not team.strip():
            raise AccountError("teams: every team name must be non-empty text")
        name = team.strip().lower()
        if name not in result:
            result.append(name)
    return tuple(result)


def _clean_name(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountError(f"{field_name}: must not be empty")
    cleaned = value.strip()
    if len(cleaned) > MAX_NAME_LENGTH:
        raise AccountError(f"{field_name}: must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def check_password_policy(password: str) -> None:
    """Raise `AccountError` unless the password is at least 12 characters. The message does not
    repeat the password."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"password: must be at least {MIN_PASSWORD_LENGTH} characters")


# --- passwords -------------------------------------------------------------------------------

_hasher = PasswordHasher()  # Argon2id with the library's default parameters


def hash_password(password: str) -> str:
    check_password_policy(password)
    return _hasher.hash(password)


@cache
def _dummy_hash() -> str:
    """A hash to verify against when the email is unknown, so that both cases cost the same."""
    return _hasher.hash(secrets.token_urlsafe(16))


def _row_user(mapping: RowMapping) -> User:
    return User(
        id=mapping["id"],
        email=mapping["email"],
        display_name=mapping["display_name"],
        role=mapping["role"],
        teams=tuple(mapping["teams"]),
        is_active=mapping["is_active"],
        created_at=mapping["created_at"],
    )


_USER_COLUMNS = (
    app_user.c.id,
    app_user.c.email,
    app_user.c.display_name,
    app_user.c.role,
    app_user.c.teams,
    app_user.c.is_active,
    app_user.c.created_at,
)


def verify_password(engine: Engine, email: str, password: str) -> User | None:
    """The user when the email and password are right and the user is active, else None.

    An unknown email, a wrong password, and an inactive user look the same to the caller and cost
    the same hashing work. A stored hash made with older parameters is replaced on success.
    WP-16 builds sign-in on this.
    """
    try:
        wanted = normalize_email(email)
    except AccountError:
        wanted = ""
    with engine.connect() as connection:
        row = connection.execute(
            select(*_USER_COLUMNS, app_user.c.password_hash).where(app_user.c.email == wanted)
        ).first()
    stored = row.password_hash if row is not None else _dummy_hash()
    try:
        _hasher.verify(stored, password if isinstance(password, str) else "")
        matches = True
    except (VerificationError, InvalidHashError):
        matches = False
    if row is None or not matches or not row.is_active:
        return None
    if _hasher.check_needs_rehash(stored):
        with engine.begin() as connection:
            connection.execute(
                update(app_user)
                .where(app_user.c.id == row.id, app_user.c.password_hash == stored)
                .values(password_hash=_hasher.hash(password))
            )
    return _row_user(row._mapping)


# --- users -----------------------------------------------------------------------------------


def create_user(
    engine: Engine,
    email: str,
    display_name: str,
    role: str,
    teams: Iterable[str],
    password: str,
) -> User:
    """Add an active user. Validates the input, and refuses a duplicate email."""
    clean_email = normalize_email(email)
    clean_name = _clean_name("display name", display_name)
    if role not in ROLES:
        raise AccountError(f"role: use one of {', '.join(ROLES)}")
    clean_teams = normalize_teams(teams)
    password_hash = hash_password(password)
    with engine.begin() as connection:
        row = connection.execute(
            pg_insert(app_user)
            .values(
                email=clean_email,
                display_name=clean_name,
                password_hash=password_hash,
                role=role,
                teams=list(clean_teams),
            )
            .on_conflict_do_nothing(index_elements=[app_user.c.email])
            .returning(*_USER_COLUMNS)
        ).first()
    if row is None:
        raise AccountError(f"email: a user with the address {clean_email} already exists")
    return _row_user(row._mapping)


def get_user(engine: Engine, email: str) -> User | None:
    wanted = normalize_email(email)
    with engine.connect() as connection:
        row = connection.execute(select(*_USER_COLUMNS).where(app_user.c.email == wanted)).first()
    return _row_user(row._mapping) if row is not None else None


def list_users(engine: Engine) -> list[User]:
    with engine.connect() as connection:
        rows = connection.execute(select(*_USER_COLUMNS).order_by(app_user.c.id)).all()
    return [_row_user(row._mapping) for row in rows]


def deactivate_user(engine: Engine, email: str) -> User:
    """Make a user inactive: sign-in and every token of the user stop working at once.
    Doing it twice changes nothing. There is no way to delete a user (the logs refer to it)."""
    wanted = normalize_email(email)
    with engine.begin() as connection:
        row = connection.execute(
            update(app_user)
            .where(app_user.c.email == wanted)
            .values(is_active=False)
            .returning(*_USER_COLUMNS)
        ).first()
    if row is None:
        raise AccountError(f"email: no user with the address {wanted}")
    return _row_user(row._mapping)


def caller_for_user(engine: Engine, user_id: int, channel: str) -> Caller:
    """The `Caller` of a signed-in user (WP-16 calls this with the id from the session).
    Raises `AuthenticationError` for an unknown or inactive user."""
    _check_channel(channel)
    with engine.connect() as connection:
        row = connection.execute(
            select(*_USER_COLUMNS).where(app_user.c.id == user_id, app_user.c.is_active.is_(True))
        ).first()
    if row is None:
        raise AuthenticationError
    return _row_user(row._mapping).caller(channel)


# --- tokens ----------------------------------------------------------------------------------


def generate_token() -> str:
    body = base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).rstrip(b"=").decode("ascii")
    return TOKEN_PREFIX + body


def hash_token(token: str) -> str:
    """Lower-case hex SHA-256 of the whole token string: what the database stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_well_formed_token(raw: object) -> bool:
    """True when `raw` has the shape of a token. The caller strips any surrounding whitespace
    (for example from a header) before authenticating; whitespace here is malformed."""
    return isinstance(raw, str) and _TOKEN_PATTERN.fullmatch(raw) is not None


def create_token(engine: Engine, user_email: str, name: str) -> NewToken:
    """Make a token for an active user. The clear token is in the result and nowhere else."""
    wanted = normalize_email(user_email)
    clean_name = _clean_name("token name", name)
    token = generate_token()
    with engine.begin() as connection:
        user = connection.execute(
            select(app_user.c.id, app_user.c.is_active).where(app_user.c.email == wanted)
        ).first()
        if user is None:
            raise AccountError(f"user: no user with the address {wanted}")
        if not user.is_active:
            raise AccountError(f"user: {wanted} is not active")
        token_id = connection.execute(
            insert(api_token)
            .values(user_id=user.id, name=clean_name, token_hash=hash_token(token))
            .returning(api_token.c.id)
        ).scalar_one()
    return NewToken(id=token_id, name=clean_name, token=token)


def revoke_token(engine: Engine, token_id: int) -> None:
    """Revoke a token. A second revocation keeps the first time. The row stays, because the
    tool-call log refers to it."""
    with engine.begin() as connection:
        found = connection.execute(
            update(api_token)
            .where(api_token.c.id == token_id)
            .values(revoked_at=func.coalesce(api_token.c.revoked_at, func.now()))
            .returning(api_token.c.id)
        ).first()
    if found is None:
        raise AccountError(f"token: no token with id {token_id}")


def list_tokens(engine: Engine, user_email: str | None = None) -> list[TokenInfo]:
    statement = (
        select(
            api_token.c.id,
            api_token.c.name,
            app_user.c.email,
            api_token.c.created_at,
            api_token.c.last_used_at,
            api_token.c.revoked_at,
        )
        .join(app_user, app_user.c.id == api_token.c.user_id)
        .order_by(api_token.c.id)
    )
    if user_email is not None:
        statement = statement.where(app_user.c.email == normalize_email(user_email))
    with engine.connect() as connection:
        rows = connection.execute(statement).all()
    return [
        TokenInfo(r.id, r.name, r.email, r.created_at, r.last_used_at, r.revoked_at) for r in rows
    ]


def _check_channel(channel: str) -> None:
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}; use one of {', '.join(CHANNELS)}")


def authenticate_token(engine: Engine, raw: str, channel: str) -> Caller:
    """The `Caller` for a bearer token, or `AuthenticationError`.

    Every failure (wrong shape, unknown, revoked, inactive user) raises the same error with the
    same message. The shape is checked before the database is read. The lookup and the
    `last_used_at` update are one statement in one transaction, so a token revoked at the same
    moment is either used or refused, never both. The caller strips whitespace from the header.
    The result has the user's role and teams, the token's id, and the given channel; with a
    `token_id` set, `Caller.effective_role` is `viewer`.
    """
    _check_channel(channel)
    if not is_well_formed_token(raw):
        logger.info("token refused: malformed")
        raise AuthenticationError
    with engine.begin() as connection:
        row = connection.execute(
            update(api_token)
            .where(
                api_token.c.token_hash == hash_token(raw),
                api_token.c.revoked_at.is_(None),
                api_token.c.user_id == app_user.c.id,
                app_user.c.is_active.is_(True),
            )
            .values(last_used_at=func.now())
            .returning(
                api_token.c.id, app_user.c.id.label("user_id"), app_user.c.role, app_user.c.teams
            )
        ).first()
    if row is None:
        # One reason for unknown, revoked, and inactive: telling them apart would need a second
        # query that only some failures run, and the timing would show which.
        logger.info("token refused: not accepted")
        raise AuthenticationError
    return Caller(
        user_id=row.user_id,
        role=row.role,
        channel=channel,
        teams=tuple(row.teams),
        token_id=row.id,
    )
