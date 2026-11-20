"""What the web editor (WP-19, WEB-2) needs from the definition store besides the lifecycle
actions: a read of what the editor should load, and the "Check" of a text.

The types are here; the two functions are methods of `Lifecycle` (`editor_source`, `check`)
because they read the repository state the same way the actions do.

Both are read-only. `Lifecycle.check` writes nothing and takes no lock; it reads the repository
without the lock, like `editor_source`, so a write that is in progress at that moment can make
either of them fail with `CONFLICT` for a moment (the caller tries again). Neither writes an audit
row or a `tool_call_log` row: they are not tools and not lifecycle actions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from makna.definitions import layout
from makna.definitions.render import MAX_YAML_BYTES, parse_text
from makna.services.errors import invalid_input

KINDS = layout.KINDS
"""The kinds of definition an editor can open (the web layer may not import `makna.definitions`)."""
__all__ = ["MAX_YAML_BYTES", "KINDS"]

NO_PROPOSAL = "none"
"""`expected_proposal_sha256` for "the object had no proposal when I opened the editor"."""
NEW_OBJECT = "new"
"""`expected_proposal_sha256` for "the object did not exist at all when I opened the editor"."""

MAX_CHECK_ISSUES = 50
"""How many issues one Check returns (the count of all of them is returned too)."""

_SHA256 = re.compile(r"[0-9a-f]{64}")


def check_expectation(value: object) -> str | None:
    """`value` if it can be an `expected_proposal_sha256` (a SHA-256 in lower-case hex, or one of
    the two fixed words), None for None; anything else is `INVALID_INPUT`."""
    if value is None:
        return None
    if isinstance(value, str) and (value in (NO_PROPOSAL, NEW_OBJECT) or _SHA256.fullmatch(value)):
        return value
    raise invalid_input(
        "expected_proposal_sha256",
        "the SHA-256 of the proposal as the editor showed it, or 'none' or 'new'",
    )


@dataclass(frozen=True)
class EditorSource:
    """What the editor loads for an existing object.

    `text` is the proposal's text when there is a proposal (as stored: the canonical form when
    Makna wrote it), else the official file's text. `official_text` is the official version in
    the canonical form of a proposal (status `draft`, no deprecation), the base of the difference
    view; it is None for an object that has no official version yet.
    """

    object_id: str
    text: str
    loaded: Literal["proposal", "official"]
    status: str | None
    """The status of what is loaded (`draft`, `in_review`, `approved`, `deprecated`), or None
    when the proposal cannot be read as a definition."""
    version: int | None
    """The version of the official object (None for a new object); a proposal carries it."""
    official_text: str | None
    proposal_sha256: str
    """SHA-256 of the proposal file's bytes, or `NO_PROPOSAL`: the value the form sends back as
    `expected_proposal_sha256`, so that a save never overwrites a proposal changed meanwhile."""
    blocked: str | None
    """Why the caller cannot save changes now (a deprecated object, a proposal made outside
    Makna in the official file, a proposal of another author), or None. Fixed text."""
    external: bool
    """True when the proposal's content came from a commit made outside Makna."""
    can_withdraw: bool = False
    """True when there is a proposal and `Lifecycle.withdraw` would allow this caller to withdraw
    it (its author, or an approver or administrator)."""


@dataclass(frozen=True)
class CheckIssue:
    """One problem found by Check: the rule (`V01` to `V23`), the line in the posted text (None
    when the rule has no line), the file it is in, and a message."""

    rule: str
    line: int | None
    file: str
    message: str
    own: bool
    """True for a problem in the object's own text; False for a problem the change causes in
    another definition (approval checks every official definition, so it would block there)."""


@dataclass(frozen=True)
class CheckResult:
    issues: tuple[CheckIssue, ...]
    count: int
    """How many problems there are in all; `issues` holds at most `MAX_CHECK_ISSUES`."""
    exists: bool
    """True when an object with this id already exists (official or proposal)."""

    @property
    def ok(self) -> bool:
        return self.count == 0


def is_object_id(kind: str, name: str) -> bool:
    """True when `kind:name` is the id of a definition object (a known kind, a well-formed name)."""
    return layout.parse_object_id(f"{kind}:{name}") is not None


def name_in(text: str) -> tuple[str | None, list[tuple[str, int | None, str]]]:
    """The name a text gives itself, or the V01 problems (rule, line, message) that stop it."""
    definition, problems = parse_text(text)
    if definition is None:
        return None, [(i.code, i.line, i.message) for i in problems]
    return definition.name, []
