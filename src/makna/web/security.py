"""The pieces of sign-in that have no HTTP in them: the signed cookies, the CSRF comparison, the
`next` path check, and the sign-in throttle (ARCHITECTURE section 13).

Two signed cookies exist, and a signature made for one never verifies as the other (each has its
own salt):

- the **session** cookie `makna_session`: the user's id, the time it was issued, and a CSRF secret.
  Nothing else. A role, teams, an email, or anything about a password never goes in it, so the
  user store decides all of those on every request;
- the **login** cookie `makna_login`: only a CSRF secret, for the sign-in form, which has no
  session yet. It lives 15 minutes and is sent to `/login` only.

Both are signed (not encrypted) with a key derived from `MAKNA_SECRET_KEY`, so their content is
readable by the browser's owner and cannot be changed by anyone.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from itsdangerous import BadData, URLSafeSerializer

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CookieNames:
    """The names of the two cookies. Outside development mode they carry the `__Host-` prefix, so
    a browser accepts them only with `Secure`, `Path=/`, and no `Domain`, and refuses one that a
    sibling subdomain tries to plant. Development mode is served over plain http, where a
    `Secure` cookie is not sent back, so there the names are plain."""

    session: str
    login: str

    @classmethod
    def for_mode(cls, development: bool) -> CookieNames:
        prefix = "" if development else "__Host-"
        return cls(prefix + "makna_session", prefix + "makna_login")


SESSION_MAX_AGE = 8 * 60 * 60
"""Seconds a session lives, from the time it was issued. It is never extended."""
LOGIN_MAX_AGE = 15 * 60
_CLOCK_SKEW = 60
CSRF_BYTES = 32
_CSRF_LENGTH = 43  # 32 random bytes in URL-safe base64 without padding
MAX_NEXT_LENGTH = 512

_SESSION_SALT = "makna.web.session.v1"
_LOGIN_SALT = "makna.web.login.v1"
_KEY_CONTEXT = b"makna.web.cookies.v1"
_SIGNER = {"digest_method": hashlib.sha256}  # HMAC-SHA256, not itsdangerous's default SHA-1


def new_csrf_secret() -> str:
    return secrets.token_urlsafe(CSRF_BYTES)


def tokens_match(given: str, expected: str) -> bool:
    """Constant-time comparison of a CSRF token with the secret. Empty never matches."""
    if not given or not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


@dataclass(frozen=True)
class Session:
    """What the session cookie holds."""

    user_id: int
    issued_at: int
    csrf: str = field(repr=False)


def _valid_secret(value: object) -> bool:
    return isinstance(value, str) and len(value) == _CSRF_LENGTH


def _valid_time(value: object, now: float) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value <= now + _CLOCK_SKEW
        and now - value <= SESSION_MAX_AGE
    )


class CookieCodec:
    """Signs and verifies the two cookies. `secret_key` is `MAKNA_SECRET_KEY`; the signing key is
    derived from it, so the raw value never signs anything directly."""

    def __init__(self, secret_key: str, clock: Callable[[], float] = time.time) -> None:
        key = hmac.new(secret_key.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()
        self._session = URLSafeSerializer(key, salt=_SESSION_SALT, signer_kwargs=_SIGNER)
        self._login = URLSafeSerializer(key, salt=_LOGIN_SALT, signer_kwargs=_SIGNER)
        self._clock = clock

    # --- session ---

    def issue_session(self, user_id: int) -> tuple[str, Session]:
        """A new session for `user_id` with a new CSRF secret: the value for the cookie and the
        session it holds. Signing in always calls this, so no earlier session is ever reused."""
        session = Session(user_id, int(self._clock()), new_csrf_secret())
        value = self._session.dumps(
            {"u": session.user_id, "iat": session.issued_at, "csrf": session.csrf}
        )
        return str(value), session

    def load_session(self, value: str | None) -> Session | None:
        """The session in a cookie value, or None for a missing, forged, malformed, too old, or
        future-dated one. The age counts from `issued_at`; nothing renews it."""
        if not value or len(value) > 1024:
            return None
        try:
            data = self._session.loads(value)
        except BadData:
            return None
        if not isinstance(data, dict) or set(data) != {"u", "iat", "csrf"}:
            return None
        user_id, issued_at, csrf = data["u"], data["iat"], data["csrf"]
        if type(user_id) is not int or user_id < 1:
            return None
        if not _valid_time(issued_at, self._clock()) or not _valid_secret(csrf):
            return None
        return Session(user_id, issued_at, csrf)

    # --- login form ---

    def issue_login(self) -> tuple[str, str]:
        """The cookie value and the CSRF secret of a sign-in form."""
        secret = new_csrf_secret()
        value = self._login.dumps({"iat": int(self._clock()), "csrf": secret})
        return str(value), secret

    def load_login(self, value: str | None) -> str | None:
        """The CSRF secret of a login cookie, or None when it is missing, forged, or too old."""
        if not value or len(value) > 1024:
            return None
        try:
            data = self._login.loads(value)
        except BadData:
            return None
        if not isinstance(data, dict) or set(data) != {"iat", "csrf"}:
            return None
        issued_at = data["iat"]
        now = self._clock()
        if (
            not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or issued_at > now + _CLOCK_SKEW
            or now - issued_at > LOGIN_MAX_AGE
            or not _valid_secret(data["csrf"])
        ):
            return None
        return str(data["csrf"])


def safe_next(value: object) -> str:
    """The path to go to after sign-in: `value` when it is a local path, else `/`.

    A local path starts with exactly one `/`, has no scheme or host, no backslash (some browsers
    read it as a slash), no control character, and is not unreasonably long. Anything else,
    including `//host`, `/\\host`, `http://host`, `javascript:...`, and non-text, becomes `/`.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_NEXT_LENGTH:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(char) < 32 or ord(char) == 127 or ord(char) > 126 for char in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


class LoginThrottle:
    """Refuses a sign-in attempt after five attempts in one minute for one email.

    - `reserve` checks and counts in one step under a lock, **before** the password is checked, so
      parallel attempts cannot all pass the check: the sixth within the minute is refused before
      any hash is made. A reservation lasts one minute.
    - A failed attempt keeps its reservation. A successful one gives its own back (`release`), so
      signing in costs the user nothing, but it never removes the reservation of another attempt:
      a success does not hand an attacker fresh guesses.
    - The key is the normalised email, whether or not a user has it, so the refusal says nothing
      about which emails exist. It is stored as a SHA-256 digest, not as the typed text.
    - The counters live in this process's memory: each worker process has its own, and a restart
      clears them. A key is dropped only when its minute is over. At most `max_keys` emails are
      kept; when all of them are still inside their minute, a new email is refused (fail closed)
      and one warning is logged (it names no email).
    """

    def __init__(
        self,
        *,
        limit: int = 5,
        window: float = 60.0,
        max_keys: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        self._clock = clock
        self._attempts: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()
        self._next_ticket = 0
        self._full_warned = False
        self._lock = threading.Lock()

    @staticmethod
    def key(email: str) -> str:
        text = email.strip().lower()[:1000]
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    def _live(self, key: str, now: float) -> deque[tuple[float, int]] | None:
        attempts = self._attempts.get(key)
        if attempts is None:
            return None
        while attempts and now - attempts[0][0] >= self._window:
            attempts.popleft()
        if not attempts:
            del self._attempts[key]
            return None
        return attempts

    def _drop_expired(self, now: float) -> None:
        """Keys are in order of their last activity, so the expired ones are at the front."""
        while self._attempts:
            key, attempts = next(iter(self._attempts.items()))
            if now - attempts[-1][0] < self._window:
                return
            del self._attempts[key]

    def reserve(self, email: str) -> int | None:
        """Count an attempt for this email: a ticket, or None when it is refused."""
        key = self.key(email)
        with self._lock:
            now = self._clock()
            attempts = self._live(key, now)
            if attempts is None:
                if len(self._attempts) >= self._max_keys:
                    self._drop_expired(now)
                if len(self._attempts) >= self._max_keys:
                    if not self._full_warned:
                        self._full_warned = True
                        logger.warning(
                            "sign-in throttle: all %d slots are inside their minute; new "
                            "emails are refused until some run out",
                            self._max_keys,
                        )
                    return None
                self._full_warned = False
                attempts = deque()
                self._attempts[key] = attempts
            elif len(attempts) >= self._limit:
                return None
            self._next_ticket += 1
            attempts.append((now, self._next_ticket))
            self._attempts.move_to_end(key)
            return self._next_ticket

    def release(self, email: str, ticket: int) -> None:
        """Give back the reservation of an attempt that was not a failure (a success, or an
        outage of the user store)."""
        key = self.key(email)
        with self._lock:
            attempts = self._attempts.get(key)
            if attempts is None:
                return
            remaining = deque(item for item in attempts if item[1] != ticket)
            if remaining:
                self._attempts[key] = remaining
            else:
                del self._attempts[key]
