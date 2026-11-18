"""The definition store: what the repository says, checked against the audit log (section 10).

This module reads a definitions git repository and decides, for every file, **what is official**.
Nothing about git history is trusted unless the application recorded it:

- A commit is an *application commit* only if its sha is in `audit_event` (any action but
  `external_change`), or it matches an open intent row: the files of the commit are exactly the
  files of the intent, with the SHA-256 the intent recorded, by the author the intent names.
  Trailers and messages prove nothing; they are never read.
- The official content of a file is its content in the **last application commit that touched
  it** (`git show <sha>:<path>`). The working tree and later commits made any other way are never
  served as approved.
- Any other commit is an *external change*. The content it leaves in a file becomes a proposal
  with status `in_review` (`external:<author email>` as its origin) and its definition errors,
  while the official content stays as it was. A new file added by such a commit is a proposal,
  whatever status it says.

It also owns the advisory lock that makes one write at a time (and the sync), the intent rows,
and `sync`, which `makna index sync` and the application start call to complete interrupted
actions, log external changes, and rebuild the index from the repository.

**The lock** is a PostgreSQL session-level advisory lock (`pg_try_advisory_lock`) on one fixed key.
A transaction-level lock cannot do the job: the intent row must be committed before git is
touched, and the lock has to stay held across that commit, the git commit, and the index
transaction. The lock is therefore taken on a connection of its own, held until the action is done,
and released with `pg_advisory_unlock`; PostgreSQL also drops it when the connection ends, so a
crashed process cannot keep it. A waiting caller holds no connection: it tries, returns the
connection to the pool, and tries again every 50 ms, so waiting threads cannot use up the pool
(the holder needs connections too). Threads of one process and separate processes serialise the
same way, because the lock lives in the database.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from makna.definitions import layout
from makna.definitions.issues import Issue
from makna.definitions.layout import ObjectRef
from makna.definitions.loader import (
    PROPOSAL_SUFFIX,
    DefinitionFile,
    DefinitionSet,
    load_text,
)
from makna.definitions.render import sha256
from makna.definitions.repo import COMMITTER_EMAIL, Commit, GitRepo, RepoError
from makna.index import indexer
from makna.index.tables import app_user, audit_event, index_state
from makna.model.definitions import AnyDefinition, Metric
from makna.model.objects import DefinitionObject
from makna.model.search import EmbeddingProvider
from makna.services.errors import LifecycleErrorCode, ServiceError
from makna.services.examples import validate_for_build

logger = logging.getLogger(__name__)

ADVISORY_LOCK_KEY = 0x4D414B4E41444546
"""The 64-bit key of the one lock the write path and the sync take (the bytes of `MAKNADEF`)."""
LOCK_WAIT_SECONDS = 60.0
LOCK_POLL_SECONDS = 0.05

EXTERNAL_ACTION = "external_change"
SEED_ACTION = "seed"
SEED_OBJECT = "repository"
APPROVAL_ACTIONS = ("approve", SEED_ACTION)
PROPOSAL_STATUSES = ("draft", "in_review")
MAX_ISSUES_SHOWN = 20


class StoreError(Exception):
    """The repository cannot be indexed. `issues` are the definition errors found; the message
    never holds git's output or file content."""

    def __init__(self, message: str, issues: tuple[Issue, ...] = ()) -> None:
        super().__init__(message)
        self.issues = issues


# --- the lock ------------------------------------------------------------------------------


@contextmanager
def advisory_lock(engine: Engine, *, wait_seconds: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    """Hold the one write lock for the body. Raises `ServiceError(CONFLICT)` after
    `wait_seconds` without getting it."""
    deadline = time.monotonic() + wait_seconds
    while True:
        connection = engine.connect()
        try:
            got = bool(
                connection.execute(
                    select(func.pg_try_advisory_lock(ADVISORY_LOCK_KEY))
                ).scalar_one()
            )
            connection.commit()
        except BaseException:
            connection.invalidate()
            connection.close()
            raise
        if got:
            break
        connection.close()  # waiting holds no connection
        if time.monotonic() >= deadline:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "Another change to the definitions is in progress. Try again in a moment.",
            )
        time.sleep(LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        try:
            connection.execute(select(func.pg_advisory_unlock(ADVISORY_LOCK_KEY)))
            connection.commit()
            connection.close()
        except Exception:
            # A connection that may still hold the lock must not go back to the pool: closing the
            # real connection makes PostgreSQL drop the lock.
            connection.invalidate()
            connection.close()


# --- the audit log -------------------------------------------------------------------------


@dataclass(frozen=True)
class Intent:
    """An `audit_event` row written before its commit: `commit_sha` is still empty."""

    id: int
    action: str
    object_id: str
    actor_user_id: int | None
    files: Mapping[str, str | None]


@dataclass(frozen=True)
class AuditView:
    """What the audit log says about commits: which are application commits, what they did, the
    open intents, and which external changes are logged."""

    applied: Mapping[str, tuple[tuple[str, str], ...]]
    """`sha -> ((object_id, action), ...)` for application commits (never `external_change`)."""
    intents: tuple[Intent, ...]
    external_logged: frozenset[tuple[str, str]]
    """`(sha, object_id)` of the `external_change` rows."""


def read_audit(connection: Connection) -> AuditView:
    applied: dict[str, list[tuple[str, str]]] = defaultdict(list)
    intents: list[Intent] = []
    logged: set[tuple[str, str]] = set()
    rows = connection.execute(
        select(
            audit_event.c.id,
            audit_event.c.action,
            audit_event.c.object_id,
            audit_event.c.actor_user_id,
            audit_event.c.commit_sha,
            audit_event.c.files,
        ).order_by(audit_event.c.id)
    )
    for row in rows:
        if row.commit_sha is None or row.commit_sha == "":
            files = row.files if isinstance(row.files, dict) else {}
            intents.append(Intent(row.id, row.action, row.object_id, row.actor_user_id, files))
        elif row.action == EXTERNAL_ACTION:
            logged.add((row.commit_sha, row.object_id))
        else:
            applied[row.commit_sha].append((row.object_id, row.action))
    return AuditView(
        {sha: tuple(items) for sha, items in applied.items()}, tuple(intents), frozenset(logged)
    )


def user_emails(connection: Connection) -> dict[int, str]:
    return {
        row.id: row.email for row in connection.execute(select(app_user.c.id, app_user.c.email))
    }


# --- reading the repository ----------------------------------------------------------------


@dataclass(frozen=True)
class ExternalChange:
    """A commit made outside the application, as it touched one object."""

    sha: str
    author_name: str
    author_email: str
    object_id: str
    paths: tuple[str, ...]


@dataclass
class ObjectState:
    """Everything the store knows about one object, read from git."""

    ref: ObjectRef
    domain: str
    official: AnyDefinition | None = None
    official_path: str | None = None
    official_commit: str | None = None
    official_bytes: bytes | None = None
    proposal: AnyDefinition | None = None
    proposal_file: DefinitionFile | None = None
    proposal_path: str | None = None
    """Where the proposal file is, or would be (`<name>.draft.yaml`)."""
    proposal_source: str | None = None
    """The path the proposal's content is at in `HEAD`: `proposal_path`, or the official file
    when an outside commit changed that."""
    proposal_commit: str | None = None
    proposal_bytes: bytes | None = None
    external_author: str | None = None
    """The author's email when the proposal's content came from an outside commit."""
    unreadable: tuple[Issue, ...] = ()
    """Why a proposal file could not be read as a definition (it is then not indexed)."""
    authors: tuple[str, ...] = ()
    """Lower-case emails of the authors of every commit on the object since it was last approved."""
    errors: tuple[Issue, ...] = ()

    @property
    def id(self) -> str:
        return self.ref.id

    @property
    def origin(self) -> str | None:
        return f"external:{self.external_author}" if self.external_author is not None else None

    @property
    def in_official_file(self) -> bool:
        """True when the proposal's content is in the official file's place (an outside commit
        left it there), so that rejecting or withdrawing it must put the official file back."""
        return self.proposal_source is not None and self.proposal_source != self.proposal_path


@dataclass
class RepoState:
    head: str | None
    objects: dict[str, ObjectState] = field(default_factory=dict)
    official_files: list[DefinitionFile] = field(default_factory=list)
    official_load_issues: list[Issue] = field(default_factory=list)
    proposal_files: list[DefinitionFile] = field(default_factory=list)
    matched: dict[int, str] = field(default_factory=dict)
    """Open intent id -> the commit that completes it."""
    dead: list[int] = field(default_factory=list)
    """Open intents that no commit matches."""
    externals: list[ExternalChange] = field(default_factory=list)
    errors: dict[str, tuple[Issue, ...]] = field(default_factory=dict)

    def gate(self) -> list[Issue]:
        """The build gate (C-1, section 5.3) on the official files alone."""
        return validate_for_build(
            DefinitionSet(Path("."), list(self.official_files), list(self.official_load_issues))
        )

    def definition_objects(self) -> list[DefinitionObject]:
        objects: list[DefinitionObject] = []
        for key in sorted(self.objects):
            item = self.objects[key]
            if item.official is None and item.proposal is None:
                continue
            errors = tuple((i.code, i.line, i.message) for i in self.errors.get(key, ()))
            objects.append(
                DefinitionObject(
                    official=item.official,
                    proposal=item.proposal,
                    official_path=item.official_path,
                    proposal_path=item.proposal_path if item.proposal is not None else None,
                    official_commit=item.official_commit,
                    proposal_commit=item.proposal_commit if item.proposal is not None else None,
                    proposal_origin=item.origin if item.proposal is not None else None,
                    proposal_errors=errors if item.proposal is not None else None,
                )
            )
        return objects


def _decode(path: str, data: bytes) -> tuple[DefinitionFile, list[Issue]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return DefinitionFile(path, path.endswith(PROPOSAL_SUFFIX), None), [
            Issue(path, None, "V01", "not valid UTF-8 text")
        ]
    return load_text(path, text)


def _force_proposal(
    file: DefinitionFile, path: str, *, external_email: str | None
) -> DefinitionFile:
    """The file as a proposal at `path`: an outside commit's content is `in_review` whatever it
    says, and a metric records where it came from in `drafted_by`."""
    definition = file.definition
    assert definition is not None
    if external_email is not None:
        update_fields: dict[str, object] = {"status": "in_review"}
        if isinstance(definition, Metric):
            update_fields["drafted_by"] = f"external:{external_email}"
        definition = definition.model_copy(update=update_fields)
    elif definition.status not in PROPOSAL_STATUSES:
        definition = definition.model_copy(update={"status": "draft"})
    return DefinitionFile(path, True, definition, file.lines)


def _match_intents(
    repo: GitRepo,
    commits: list[Commit],
    trusted: set[str],
    intents: tuple[Intent, ...],
    emails: Mapping[int, str],
) -> dict[int, str]:
    """Open intents completed by a commit: same files, same SHA-256 of each, same author."""
    free = [c for c in commits if c.sha not in trusted]
    matched: dict[int, str] = {}
    for intent in intents:
        if not intent.files:
            continue
        author = (
            emails.get(intent.actor_user_id)
            if intent.actor_user_id is not None
            else COMMITTER_EMAIL
        )
        wanted = set(intent.files)
        for commit in free:
            if commit.sha in matched.values() or {p for _, p in commit.changes} != wanted:
                continue
            if author is None or commit.author_email.casefold() != author.casefold():
                continue
            if any(
                (status == "D") != (intent.files[path] is None) for status, path in commit.changes
            ):
                continue
            kept = [(commit.sha, p) for status, p in commit.changes if status != "D"]
            blobs = repo.read_blobs(kept)
            if all(
                (data := blobs[(commit.sha, p)]) is not None and sha256(data) == intent.files[p]
                for _, p in kept
            ):
                matched[intent.id] = commit.sha
                break
    return matched


def read_state(repo: GitRepo, audit: AuditView, emails: Mapping[int, str]) -> RepoState:
    """Read the whole repository and classify it. Reads only commits (`git log`, `cat-file`);
    the working tree is never looked at."""
    commits = repo.log()
    if not commits:
        return RepoState(None, dead=[i.id for i in audit.intents])
    head = commits[-1].sha
    trusted = set(audit.applied)
    state = RepoState(head)
    state.matched = _match_intents(repo, commits, trusted, audit.intents, emails)
    state.dead = [i.id for i in audit.intents if i.id not in state.matched]
    trusted |= set(state.matched.values())
    actions: dict[str, set[str]] = defaultdict(set)  # sha -> actions of that application commit
    for sha, items in audit.applied.items():
        actions[sha].update(action for _, action in items)
    for intent in audit.intents:
        if intent.id in state.matched:
            actions[state.matched[intent.id]].add(intent.action)

    # Walk the history once: the last touch of each path (any commit, application commit), the
    # commits that touched each object, and the external changes.
    last_any: dict[str, tuple[int, Commit, str]] = {}
    last_app: dict[str, tuple[int, Commit, str]] = {}
    touches: dict[ObjectRef, list[int]] = defaultdict(list)
    for index, commit in enumerate(commits):
        by_object: dict[ObjectRef, list[str]] = defaultdict(list)
        for status, path in commit.changes:
            info = layout.parse_path(path)
            if info is None:
                continue
            last_any[path] = (index, commit, status)
            if commit.sha in trusted:
                last_app[path] = (index, commit, status)
            by_object[info.ref].append(path)
        for ref, paths in by_object.items():
            touches[ref].append(index)
            if commit.sha not in trusted:
                state.externals.append(
                    ExternalChange(
                        commit.sha,
                        commit.author_name,
                        commit.author_email,
                        ref.id,
                        tuple(sorted(paths)),
                    )
                )

    at_head = {p for p in repo.regular_files(head) if layout.parse_path(p) is not None}
    # Official files: the last application commit that wrote them and did not delete them.
    official_at: dict[ObjectRef, str] = {}
    for path, (_, _, status) in sorted(last_app.items()):
        info = layout.parse_path(path)
        if info is not None and not info.is_proposal and status != "D":
            official_at.setdefault(info.ref, path)
    wanted: list[tuple[str, str]] = [(last_app[p][1].sha, p) for p in official_at.values()]
    # Content at HEAD of every candidate for a proposal.
    candidates: dict[ObjectRef, list[str]] = defaultdict(list)
    for path in sorted(at_head):
        info = layout.parse_path(path)
        assert info is not None
        candidates[info.ref].append(path)
        wanted.append((head, path))
    for path in at_head:  # for a revert check: the last application version of a changed proposal
        if path in last_app and last_app[path][2] != "D":
            wanted.append((last_app[path][1].sha, path))
    blobs = repo.read_blobs(wanted)

    refs = sorted(set(official_at) | set(candidates), key=lambda r: r.id)
    for ref in refs:
        official_path = official_at.get(ref)
        paths_here = candidates.get(ref, [])
        domain_source = official_path or (paths_here[0] if paths_here else None)
        assert domain_source is not None
        info = layout.parse_path(domain_source)
        assert info is not None
        item = ObjectState(ref, info.domain)
        if official_path is not None:
            commit = last_app[official_path][1]
            data = blobs[(commit.sha, official_path)]
            assert data is not None
            item.official_path = official_path
            item.official_commit = commit.sha
            item.official_bytes = data
            file, issues = _decode(official_path, data)
            state.official_load_issues.extend(issues)
            if file.definition is not None:
                state.official_files.append(file)
                item.official = file.definition
        _read_proposal(item, paths_here, blobs, head, last_any, last_app, trusted)
        # Authors: everyone who authored a commit on the object since it was last approved.
        boundary = -1
        for index in touches.get(ref, []):
            if actions.get(commits[index].sha, set()) & set(APPROVAL_ACTIONS):
                boundary = index
        item.authors = tuple(
            sorted(
                {commits[i].author_email.casefold() for i in touches.get(ref, []) if i > boundary}
            )
        )
        state.objects[ref.id] = item
        if item.proposal is not None:
            assert item.proposal_path is not None and item.proposal_source is not None
            assert item.proposal_file is not None
            state.proposal_files.append(item.proposal_file)
    _validate_proposals(state)
    return state


def _read_proposal(
    item: ObjectState,
    paths_here: list[str],
    blobs: Mapping[tuple[str, str], bytes | None],
    head: str,
    last_any: Mapping[str, tuple[int, Commit, str]],
    last_app: Mapping[str, tuple[int, Commit, str]],
    trusted: set[str],
) -> None:
    ref = item.ref
    proposal_path = layout.object_path(ref, item.domain, proposal=True)
    item.proposal_path = proposal_path
    drafts = [p for p in paths_here if layout.parse_path(p).is_proposal]  # type: ignore[union-attr]
    source: str | None = None
    external = False
    if drafts:
        source = drafts[0]
        _, commit, _ = last_any[source]
        data = blobs[(head, source)]
        if commit.sha not in trusted:
            before = last_app.get(source)
            same = (
                before is not None
                and before[2] != "D"
                and blobs.get((before[1].sha, source)) == data
            )
            external = not same
            if same:
                commit = before[1]  # type: ignore[index]
        item.proposal_commit = commit.sha
        item.external_author = commit.author_email if external else None
    else:
        for (
            path
        ) in paths_here:  # an official-looking file whose content the application did not write
            if path == item.official_path and blobs[(head, path)] == item.official_bytes:
                continue
            if blobs[(head, path)] is None:
                continue
            source = path
            external = True
            _, commit, _ = last_any[path]
            item.proposal_commit = commit.sha
            item.external_author = commit.author_email
            break
    if source is None:
        return
    data = blobs[(head, source)]
    assert data is not None
    item.proposal_source = source
    item.proposal_bytes = data
    file, issues = _decode(proposal_path, data)
    if file.definition is None:
        item.unreadable = tuple(issues)
        return
    item.proposal_file = _force_proposal(file, proposal_path, external_email=item.external_author)
    item.proposal = item.proposal_file.definition


def _validate_proposals(state: RepoState) -> None:
    """Validate the official files with the proposals added, and give each proposal the errors in
    its own file. A proposal is checked as if it were at its proposal path."""
    full = DefinitionSet(
        Path("."),
        [*state.official_files, *state.proposal_files],
        list(state.official_load_issues),
    )
    by_path = {
        o.proposal_path: key
        for key, o in state.objects.items()
        if o.proposal is not None and o.proposal_path is not None
    }
    found: dict[str, list[Issue]] = defaultdict(list)
    for issue in validate_for_build(full):
        key = by_path.get(issue.file)
        if key is not None and issue.file.endswith(PROPOSAL_SUFFIX):
            found[key].append(issue)
    for key, item in state.objects.items():
        if item.proposal is not None:
            item.errors = tuple(found.get(key, ()))
        state.errors[key] = item.errors


# --- the index -----------------------------------------------------------------------------


def _external_rows(state: RepoState, audit: AuditView) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for change in state.externals:
        key = (change.sha, change.object_id)
        if key in audit.external_logged or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "actor_label": f"external:{change.author_email}"[:200],
                "action": EXTERNAL_ACTION,
                "object_id": change.object_id,
                "commit_sha": change.sha,
                "to_status": "in_review",
                "reason": f"changed outside Makna by {_clean(change.author_name)}"[:500],
            }
        )
    return rows


def _clean(text: str) -> str:
    return re.sub(r"[^\w .@'-]", "", text)[:100]


def reindex(
    engine: Engine,
    provider: EmbeddingProvider,
    state: RepoState,
    audit: AuditView,
    *,
    derive: indexer.Derive | None,
    extra: Callable[[Connection], None] | None = None,
) -> indexer.IndexResult:
    """Write the index from `state` and, in the same transaction, the audit facts: the commit of
    each completed intent, the dead intents removed, the external changes logged, and
    `last_indexed_commit` (section 10, step 4). Raises `StoreError` if the official files fail the
    build gate; then nothing is written."""
    problems = state.gate()
    if problems:
        raise StoreError(
            f"the official definitions have {len(problems)} problem(s), so nothing was indexed",
            tuple(problems),
        )
    objects = state.definition_objects()
    if not objects:
        raise StoreError("the repository holds no definition objects; nothing was indexed")
    external = _external_rows(state, audit)

    def within(connection: Connection) -> None:
        for intent_id, sha in state.matched.items():
            connection.execute(
                update(audit_event)
                .where(audit_event.c.id == intent_id, audit_event.c.commit_sha.is_(None))
                .values(commit_sha=sha)
            )
        if state.dead:
            connection.execute(
                delete(audit_event).where(
                    audit_event.c.id.in_(state.dead), audit_event.c.commit_sha.is_(None)
                )
            )
        if external:
            connection.execute(
                pg_insert(audit_event)
                .values(external)
                .on_conflict_do_nothing(constraint="uq_audit_event_commit_object_action")
            )
        if state.head is not None:
            current = connection.execute(
                select(index_state.c.value).where(index_state.c.key == "last_indexed_commit")
            ).scalar_one_or_none()
            if current is None:
                connection.execute(
                    insert(index_state).values(key="last_indexed_commit", value=state.head)
                )
            elif current != state.head:
                connection.execute(
                    update(index_state)
                    .where(index_state.c.key == "last_indexed_commit")
                    .values(value=state.head)
                )
        if extra is not None:
            extra(connection)

    return indexer.build(engine, provider, objects, derive=derive, within=within)


def sync(
    engine: Engine,
    provider: EmbeddingProvider,
    repo: GitRepo,
    *,
    derive: indexer.Derive | None,
    locked: bool = False,
) -> tuple[RepoState, indexer.IndexResult]:
    """Complete interrupted actions, log external changes, and rebuild the index from `HEAD`.

    Idempotent: a second run finds nothing to change and writes nothing. `locked` says the
    caller already holds the advisory lock. Raises `StoreError` (the repository or its official
    definitions cannot be indexed) or `ServiceError(CONFLICT)` (the lock is busy)."""

    def run() -> tuple[RepoState, indexer.IndexResult]:
        try:
            with engine.connect() as connection:
                audit = read_audit(connection)
                emails = user_emails(connection)
            state = read_state(repo, audit, emails)
            result = reindex(engine, provider, state, audit, derive=derive)
        except RepoError as error:
            raise StoreError(f"the repository cannot be read ({error})") from None
        except SQLAlchemyError as error:
            raise StoreError(f"the database cannot be written ({type(error).__name__})") from None
        return state, result

    if locked:
        return run()
    with advisory_lock(engine):
        return run()
