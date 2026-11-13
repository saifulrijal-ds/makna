"""Who is calling (ARCHITECTURE section 11).

A `Caller` is built by the interface that authenticated the request: WP-12B turns a signed-in
user or a bearer token into one. The services trust it and never look at credentials. Tests
build it directly.
"""

from __future__ import annotations

from dataclasses import dataclass

ROLES: tuple[str, ...] = ("viewer", "curator", "approver", "admin")
CHANNELS: tuple[str, ...] = ("mcp", "web", "cli")


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Caller:
    """The user or token that makes a service call.

    - `user_id`: the user the call is made by or for. A token belongs to a user, so a token
      caller has both ids; at least one is required.
    - `token_id`: set when the call came with an API token. A token acts with at most the viewer
      permissions of its user, whatever the user's role (D-4, DR-6): `effective_role` says so,
      and the permission matrix uses the "agent token" column for it.
    - `role`: `viewer`, `curator`, `approver`, or `admin`. Anything else is a `ValueError`.
    - `teams`: the user's teams.
    - `channel`: `mcp`, `web`, or `cli`. Anything else is a `ValueError`.
    """

    user_id: int | None
    role: str
    channel: str
    teams: tuple[str, ...] = ()
    token_id: int | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}; use one of {', '.join(ROLES)}")
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}; use one of {', '.join(CHANNELS)}")
        for name, value in (("user_id", self.user_id), ("token_id", self.token_id)):
            if value is not None and not _is_id(value):
                raise ValueError(f"{name} must be a whole number or None, not {value!r}")
        if self.user_id is None and self.token_id is None:
            raise ValueError("a caller needs a user_id or a token_id")
        if isinstance(self.teams, str) or not all(isinstance(t, str) for t in self.teams):
            raise ValueError("teams must be a collection of team names")
        object.__setattr__(self, "teams", tuple(self.teams))

    @property
    def is_token(self) -> bool:
        return self.token_id is not None

    @property
    def effective_role(self) -> str:
        """The role the caller acts with: `viewer` for a token, the given role otherwise."""
        return "viewer" if self.is_token else self.role
