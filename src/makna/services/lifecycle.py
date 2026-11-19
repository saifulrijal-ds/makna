"""The seven lifecycle actions and the seed (ARCHITECTURE section 10): the only way a definition
becomes official.

`Lifecycle` is reached through `Services.lifecycle`. Each action takes a `Caller`, an object id,
and a reason, and does the same steps:

1. take the advisory lock (`makna.services.store`); read the repository and the audit log, which
   is the state the permission is checked against, so a race cannot approve a proposal that
   changed in between; check permission (with the role and teams of the user row read now, not
   those the caller was built with), validate, and (for approve and deprecate) check that the
   official files still pass the build gate with the change;
2. write an `audit_event` **intent** row: action, object, actor, reason, and the SHA-256 of every
   file the action writes or deletes;
3. build the commit from the checked bytes with git plumbing and a private index
   (`GitRepo.commit_files`), with the user as author, and move `HEAD` to it only if `HEAD` is still
   where it was read. The working tree is not read; afterwards it is brought in line, best effort;
4. in one database transaction: index the repository (so every object whose derived facts depend
   on the change is refreshed), set `commit_sha` on the intent, bump `definitions_version`,
   and set `last_indexed_commit`.

The lock is checked before steps 2 and 3 and before step 4: a session-level lock is lost with its
connection, and an action must not go on without it.

Step 4 is the same code as `makna index sync`, with the commit this action made as its own
intent's commit (so a retry after a crash completes its own intent and not an older one).

If git fails in step 3, nothing was committed: the intent row is kept and marked failed
(`failed_at`), so the attempt leaves a trace, and the call fails. If step 4 fails the commit
exists and the intent stays open; the call fails with a message that says to run
`makna index sync`, which finds the commit by its files and their hashes.

A refusal (permission, validation, state) happens before step 2: no file is written and no audit
row exists. What the application writes is never the text a caller sent: the YAML is parsed,
checked as a model, given its `status` and `version` by the application, and written in one
canonical form (`makna.definitions.render`). The path of a file comes only from a checked object id
and an existing domain (`makna.definitions.layout`). Errors never carry stored YAML, file content,
or git's output; anything unexpected is mapped to a fixed `INTERNAL` message.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Connection, Engine, func, insert, update
from sqlalchemy.exc import SQLAlchemyError

from makna.catalog import references
from makna.compiler.compile import compile_request
from makna.compiler.governance import GovernanceError
from makna.compiler.snapshot import Model
from makna.definitions import layout
from makna.definitions.issues import Issue
from makna.definitions.layout import ObjectRef
from makna.definitions.loader import DefinitionSet, load_definitions, load_text
from makna.definitions.render import MAX_YAML_BYTES, parse_text, sha256, to_yaml, with_state
from makna.definitions.repo import (
    COMMITTER_EMAIL,
    COMMITTER_NAME,
    CommitResult,
    GitRepo,
    RepoError,
    RepoMode,
    RepoUnusable,
    repository_mode,
)
from makna.index import indexer
from makna.index.indexer import catalog_sensitivity
from makna.index.tables import audit_event
from makna.model.definitions import AnyDefinition, Deprecation, Entity, GlossaryTerm, Metric
from makna.model.governance import GovernancePolicy
from makna.model.request import QueryError, QueryRequest
from makna.model.search import EmbeddingProvider
from makna.services import accounts, approval, permissions, store
from makna.services.approval import ApprovalSettings, Decision
from makna.services.caller import Caller
from makna.services.errors import (
    LifecycleErrorCode,
    PermissionDenied,
    ServiceError,
    ServiceErrorCode,
    invalid_input,
)
from makna.services.examples import validate_for_build
from makna.services.store import ObjectState, RepoState

logger = logging.getLogger(__name__)

MAX_REASON_LENGTH = 500
MAX_ISSUES_SHOWN = 20
MAX_MESSAGE_SHOWN = 300

CREATE_PROPOSAL = "create_proposal"
UPDATE_PROPOSAL = "update_proposal"
SUBMIT = "submit"
APPROVE = "approve"
REJECT = "reject"
WITHDRAW = "withdraw"
DEPRECATE = "deprecate"
ACTIONS = (CREATE_PROPOSAL, UPDATE_PROPOSAL, SUBMIT, APPROVE, REJECT, WITHDRAW, DEPRECATE)
_SUBJECT_VERB = {CREATE_PROPOSAL: "propose", UPDATE_PROPOSAL: "edit"}

READ_ONLY_MESSAGE = (
    "The definitions are read from a plain directory, so they cannot be changed here. Set "
    "MAKNA_DEFINITIONS_REPO to a git repository of its own (`init` creates one)."
)
GATE_MESSAGE = (
    "With this change the official definitions would not pass the checks of `makna validate`."
)
COMMITTED_NOT_INDEXED = (
    "The change was committed but the index could not be updated. The commit and the audit "
    "record of the action are kept; an administrator can run `makna index sync` to finish it."
)
UNEXPECTED = "The change could not be made. Nothing was changed."
REPOSITORY_MOVED = (
    "The definitions repository changed while this change was being made, so nothing was saved. "
    "Try again."
)


@dataclass(frozen=True)
class ActionResult:
    """What an action did: the object, its status now, its version, and the commit."""

    object_id: str
    status: str
    version: int | None
    commit: str
    action: str


@dataclass(frozen=True)
class _Plan:
    """What one action will do, decided under the lock."""

    files: Mapping[str, bytes | None]
    from_status: str | None
    to_status: str
    version: int | None
    """The version the object has after the action; written to `Makna-Version`."""


def check_reason(reason: object) -> str:
    """A reason: text, trimmed, one line, 1 to 500 characters, no control characters. Anything
    else is `INVALID_INPUT`. Since it is one line, it cannot add a trailer to a commit message."""
    if not isinstance(reason, str):
        raise invalid_input("reason", "text that says why")
    text = reason.strip()
    if not text:
        raise invalid_input("reason", "a reason is required for every action")
    if len(text) > MAX_REASON_LENGTH:
        raise invalid_input(
            "reason", f"at most {MAX_REASON_LENGTH} characters", maximum=MAX_REASON_LENGTH
        )
    if any(unicodedata.category(c)[0] == "C" or c in "  " for c in text):
        raise invalid_input(
            "reason", "one line of plain text, with no line break or control character"
        )
    return text


def _issues_error(message: str, issues: list[Issue]) -> ServiceError:
    shown = [
        {"rule": i.code, "line": i.line, "file": i.file, "message": i.message[:MAX_MESSAGE_SHOWN]}
        for i in issues[:MAX_ISSUES_SHOWN]
    ]
    return ServiceError(
        LifecycleErrorCode.DEFINITION_INVALID,
        message,
        {"issues": shown, "count": len(issues)},
    )


def _denied(decision: Decision) -> PermissionDenied:
    details: dict[str, Any] = {"reason": str(decision.refusal)}
    if decision.needs:
        details["needs"] = list(decision.needs)
    return PermissionDenied(decision.message, details)


def _open_folder(root: Path, parts: list[str], *, create: bool) -> int:
    """A directory descriptor for `parts` below `root`, opened one component at a time with
    `O_NOFOLLOW`, so a link at any component (even one swapped in just now) is refused (`OSError`)
    and nothing is followed out of the repository. Missing folders are made when `create`."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o755, dir_fd=descriptor)
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_file(root: Path, relative: str, data: bytes) -> None:
    """Write `data` to `relative` below `root`, never through a link and only to a regular file
    (`OSError` otherwise). The file is opened without blocking and checked with `fstat` before it
    is truncated, so a FIFO or device planted at the path cannot hang the caller (which holds the
    lock) or be written to."""
    *folders, name = relative.split("/")
    folder = _open_folder(root, folders, create=True)
    try:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=folder
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("not a regular file")
            os.ftruncate(descriptor, 0)
            with os.fdopen(os.dup(descriptor), "wb") as handle:
                handle.write(data)
        finally:
            os.close(descriptor)
    finally:
        os.close(folder)


def _remove_file(root: Path, relative: str) -> None:
    """Delete `relative` below `root`, never through a link (`OSError` if there is one)."""
    *folders, name = relative.split("/")
    folder = _open_folder(root, folders, create=False)
    try:
        os.unlink(name, dir_fd=folder)
    finally:
        os.close(folder)


def _message(
    action: str, object_id: str, reason: str, actor_id: int | None, version: int | None
) -> str:
    verb = _SUBJECT_VERB.get(action, action)
    return (
        f"{verb}({object_id}): {reason}\n\n"
        f"Makna-Action: {action}\n"
        f"Makna-Object: {object_id}\n"
        f"Makna-Actor: {actor_id if actor_id is not None else 'makna'}\n"
        f"Makna-Version: {version if version is not None else 0}\n"
    )


class Lifecycle:
    """The write path of the definition store, for one database and one repository path."""

    def __init__(
        self,
        engine: Engine,
        provider: EmbeddingProvider,
        *,
        derive: indexer.Derive | None,
        repo_path: str | None,
        settings: ApprovalSettings,
        policy: GovernancePolicy | None = None,
        dialect: str = "postgres",
    ) -> None:
        self._engine = engine
        self._provider = provider
        self._derive = derive
        self._path = Path(repo_path) if repo_path else None
        self._settings = settings
        self._policy = policy
        self._dialect: Literal["postgres", "duckdb"] = (
            "duckdb" if dialect == "duckdb" else "postgres"
        )

    # --- mode ----------------------------------------------------------------------------

    def mode(self) -> RepoMode | None:
        """How `MAKNA_DEFINITIONS_REPO` is treated: `REPOSITORY` (written by commits), `PLAIN` (a
        directory inside another repository: read-only), or None when no path is configured.
        Raises `ServiceError(CONFLICT)` when the configured path is a repository that is broken:
        it is never read as a plain directory."""
        if self._path is None:
            return None
        try:
            return repository_mode(self._path, configured=True)
        except RepoUnusable as error:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, error.message, {"unusable": error.reason}
            ) from None

    def writable(self) -> bool:
        """True when the definitions are a git repository of its own that the application writes.
        False for a plain directory (inside another repository) or no path; a broken repository
        raises, see `mode`."""
        return self.mode() is RepoMode.REPOSITORY

    def _repo(self) -> GitRepo:
        if self.mode() is not RepoMode.REPOSITORY or self._path is None:
            raise ServiceError(LifecycleErrorCode.CONFLICT, READ_ONLY_MESSAGE, {"read_only": True})
        return GitRepo(self._path)

    # --- the seven actions ---------------------------------------------------------------

    def create_proposal(
        self, caller: Caller, object_id: str, yaml_text: str, reason: str
    ) -> ActionResult:
        """A new proposal file, `draft`, for a new object or a change to an official one."""
        return self._act(caller, CREATE_PROPOSAL, object_id, reason, yaml_text=yaml_text)

    def update_proposal(
        self, caller: Caller, object_id: str, yaml_text: str, reason: str
    ) -> ActionResult:
        """Rewrite the proposal; an `in_review` proposal goes back to `draft`."""
        return self._act(caller, UPDATE_PROPOSAL, object_id, reason, yaml_text=yaml_text)

    def submit(self, caller: Caller, object_id: str, reason: str) -> ActionResult:
        return self._act(caller, SUBMIT, object_id, reason)

    def approve(
        self,
        caller: Caller,
        object_id: str,
        reason: str,
        expected_proposal_commit: str | None = None,
    ) -> ActionResult:
        """Approve the proposal. With `expected_proposal_commit` (the commit the reviewer saw),
        the action is refused with `CONFLICT` if the proposal has changed since."""
        return self._act(
            caller, APPROVE, object_id, reason, expected_proposal_commit=expected_proposal_commit
        )

    def reject(
        self,
        caller: Caller,
        object_id: str,
        reason: str,
        expected_proposal_commit: str | None = None,
    ) -> ActionResult:
        return self._act(
            caller, REJECT, object_id, reason, expected_proposal_commit=expected_proposal_commit
        )

    def withdraw(self, caller: Caller, object_id: str, reason: str) -> ActionResult:
        return self._act(caller, WITHDRAW, object_id, reason)

    def deprecate(
        self, caller: Caller, object_id: str, reason: str, replaced_by: str | None = None
    ) -> ActionResult:
        return self._act(caller, DEPRECATE, object_id, reason, replaced_by=replaced_by)

    # --- sync ----------------------------------------------------------------------------

    def sync(
        self, *, accept_rewritten_history: bool = False
    ) -> tuple[RepoState, indexer.IndexResult] | None:
        """`makna index sync`: None in plain-directory mode (there is nothing to sync). Raises
        `ServiceError(CONFLICT)` for a broken repository or a rewritten history."""
        if self.mode() is not RepoMode.REPOSITORY:
            return None
        try:
            return store.sync(
                self._engine,
                self._provider,
                self._repo(),
                derive=self._derive,
                accept_rewritten_history=accept_rewritten_history,
            )
        except store.StoreError as error:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, str(error), {"fatal": error.fatal}
            ) from None

    # --- the common flow -----------------------------------------------------------------

    def _act(
        self,
        caller: Caller,
        action: str,
        object_id: str,
        reason: str,
        *,
        yaml_text: str | None = None,
        replaced_by: str | None = None,
        expected_proposal_commit: str | None = None,
    ) -> ActionResult:
        text = check_reason(reason)
        ref = layout.parse_object_id(object_id) if isinstance(object_id, str) else None
        if ref is None:
            raise invalid_input("id", "an id of the form kind:name, for example metric:npf_ratio")
        if yaml_text is not None:
            _check_size(yaml_text)
        if expected_proposal_commit is not None and (
            not isinstance(expected_proposal_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", expected_proposal_commit) is None
        ):
            raise invalid_input("expected_proposal_commit", "a commit sha, as the review showed it")
        self._early_refusal(caller, action)
        repo = self._repo()
        try:
            with store.advisory_lock(self._engine) as lock:
                user = self._user(caller)
                # The role and teams of the user row as they are now, not those of the session.
                actor = Caller(
                    user_id=user.id, role=user.role, channel=caller.channel, teams=user.teams
                )
                self._early_refusal(actor, action)
                state = self._read(repo)
                plan = self._plan(
                    actor, user, action, ref, state, yaml_text, replaced_by, text,
                    expected_proposal_commit,
                )  # fmt: skip
                lock.check()
                intent_id = self._write_intent(user, action, ref.id, text, plan)
                made = self._commit(repo, lock, user, action, ref.id, text, plan, intent_id, state)
                self._finish(repo, lock, intent_id, made)
        except ServiceError:
            raise
        except store.StoreError as error:
            raise ServiceError(LifecycleErrorCode.CONFLICT, str(error)) from None
        except Exception as error:
            # Anything else (a database error, a deeply nested file): a fixed message, no text.
            logger.error("lifecycle: unexpected %s in %s", type(error).__name__, action)
            raise ServiceError(ServiceErrorCode.INTERNAL, UNEXPECTED) from None
        return ActionResult(ref.id, plan.to_status, plan.version, made.sha, action)

    # --- helpers -------------------------------------------------------------------------

    def _early_refusal(self, caller: Caller, action: str) -> None:
        """Refuse what no state can change, before the lock is taken."""
        if caller.is_token:
            raise _denied(
                Decision(False, approval.Refusal.TOKEN, "An API token cannot change definitions.")
            )
        decides = action in (APPROVE, REJECT, DEPRECATE)
        if decides:
            grant = permissions.approval_grant(caller)
            if grant in (permissions.Grant.NO, permissions.Grant.NOT_APPLICABLE):
                raise _denied(
                    Decision(False, approval.Refusal.ROLE, "Your role does not allow this action.")
                )
        elif not permissions.can_edit_proposals(caller):
            raise _denied(
                Decision(False, approval.Refusal.EDIT_ROLE, "Your role does not allow this action.")
            )

    def _user(self, caller: Caller) -> accounts.User:
        user = accounts.get_user_by_id(self._engine, caller.user_id) if caller.user_id else None
        if user is None or not user.is_active:
            raise _denied(
                Decision(
                    False,
                    approval.Refusal.NO_ACCOUNT,
                    "Only an active user can change definitions.",
                )
            )
        return user

    def _read(self, repo: GitRepo) -> RepoState:
        try:
            with self._engine.connect() as connection:
                audit = store.read_audit(connection)
                emails = store.user_emails(connection)
            return store.read_state(repo, audit, emails)
        except store.StoreError:
            raise
        except (RepoError, SQLAlchemyError) as error:
            logger.error("lifecycle: cannot read the repository (%s)", type(error).__name__)
            raise ServiceError(
                ServiceErrorCode.INTERNAL, "The definitions repository cannot be read."
            ) from None

    def _proposal_reference_issues(
        self,
        connection: Connection,
        state: RepoState,
        item: ObjectState,
        proposal: AnyDefinition,
    ) -> list[str]:
        """The reference issues (R01 to R04, R06, R07, and R05 for a metric) the proposed
        definition would have against the catalog as it is now, as `"CODE part"`. The open rows of
        `reference_issue` belong to the official version and are not looked at: an issue that is
        stale for the official version must not block a proposal that fixes it. Without a synced
        catalog there is nothing to check against."""
        view = references.load_view(connection)
        if not view.tables:
            return []
        ref = item.ref
        if isinstance(proposal, Entity | GlossaryTerm):
            entities = [
                o.official
                for o in state.objects.values()
                if isinstance(o.official, Entity) and o.ref != ref
            ]
            terms = [
                o.official
                for o in state.objects.values()
                if isinstance(o.official, GlossaryTerm) and o.ref != ref
            ]
            if isinstance(proposal, Entity):
                entities.append(proposal)
            else:
                terms.append(proposal)
            found = references.find_issues(entities, terms, view)
            return [f"{f.code} {f.part}" for f in found if f.object_id == ref.id]
        if isinstance(proposal, Metric) and self._policy is not None:
            return self._metric_reference_issues(connection, state, proposal)
        return []

    def _metric_reference_issues(
        self, connection: Connection, state: RepoState, proposal: Metric
    ) -> list[str]:
        """R05: the metric reads a column the catalog marks personal where personal data is not
        allowed. Found by compiling it once, as the indexer does."""
        assert self._policy is not None
        entities = [o.official for o in state.objects.values() if isinstance(o.official, Entity)]
        metrics = [
            o.official
            for o in state.objects.values()
            if isinstance(o.official, Metric) and o.ref.name != proposal.name
        ]
        approved = proposal.model_copy(update={"status": "approved"})
        model = Model.build(entities, [*metrics, approved], {}, catalog_sensitivity(connection))
        try:
            compile_request(
                model, QueryRequest(metric=proposal.name), self._dialect, governance=self._policy
            )
        except QueryError:
            return []  # refused for another reason, with a code of its own
        except GovernanceError as error:
            if error.table is None or error.column is None:
                return []
            return [f"R05 column:{error.table}.{error.column}"]
        return []

    # --- planning each action ------------------------------------------------------------

    def _plan(
        self,
        caller: Caller,
        user: accounts.User,
        action: str,
        ref: ObjectRef,
        state: RepoState,
        yaml_text: str | None,
        replaced_by: str | None,
        reason: str,
        expected_commit: str | None = None,
    ) -> _Plan:
        item = state.objects.get(ref.id)
        if action == CREATE_PROPOSAL:
            assert yaml_text is not None
            return self._plan_create(caller, user, ref, item, state, yaml_text)
        if item is None or (item.official is None and item.proposal_source is None):
            raise ServiceError(
                ServiceErrorCode.NOT_FOUND, f"There is no object with the id {ref.id}."
            )
        if action == UPDATE_PROPOSAL:
            assert yaml_text is not None
            return self._plan_update(caller, user, ref, item, state, yaml_text)
        if action == DEPRECATE:
            return self._plan_deprecate(caller, user, ref, item, state, replaced_by, reason)
        return self._plan_proposal_action(caller, user, action, ref, item, state, expected_commit)

    def _parse(self, ref: ObjectRef, yaml_text: str) -> AnyDefinition:
        definition, problems = parse_text(yaml_text)
        if definition is None:
            raise _issues_error("The YAML is not a valid definition.", problems)
        if definition.kind != ref.kind or definition.name != ref.name:
            raise invalid_input("yaml", "kind and name in the YAML must be those of the object id")
        return definition

    def _domain_of(self, definition: AnyDefinition, state: RepoState) -> str:
        domain = definition.name if definition.kind == "domain" else getattr(definition, "domain")  # noqa: B009
        known = state.objects.get(f"domain:{domain}")
        if definition.kind != "domain" and (known is None or known.official is None):
            raise invalid_input("domain", "the domain must exist (an official domain object)")
        return str(domain)

    def _plan_create(
        self,
        caller: Caller,
        user: accounts.User,
        ref: ObjectRef,
        item: ObjectState | None,
        state: RepoState,
        yaml_text: str,
    ) -> _Plan:
        proposal = self._parse(ref, yaml_text)
        domain = self._domain_of(proposal, state)
        if item is not None and item.proposal_source is not None:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "This object already has a proposal; change it with update_proposal.",
            )
        official = item.official if item is not None else None
        if official is None:
            decision = approval.may_create_new(caller, proposal)
        else:
            if item is not None and item.domain != domain:
                raise invalid_input("domain", "a change cannot move the object to another domain")
            if official.status == "deprecated":
                raise ServiceError(
                    LifecycleErrorCode.CONFLICT, "A deprecated object cannot be changed."
                )
            decision = approval.may_edit(caller, user.email, None)
        if not decision:
            raise _denied(decision)
        version = official.version if official is not None else None
        text = to_yaml(_as_proposal(proposal, "draft", version))
        path = layout.object_path(ref, domain, proposal=True)
        return _Plan(
            {path: text.encode("utf-8")}, official.status if official else None, "draft", version
        )

    def _plan_update(
        self,
        caller: Caller,
        user: accounts.User,
        ref: ObjectRef,
        item: ObjectState,
        state: RepoState,
        yaml_text: str,
    ) -> _Plan:
        if item.proposal_source is None:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, "This object has no proposal to change."
            )
        decision = approval.may_edit(caller, user.email, item.authors)
        if not decision:
            raise _denied(decision)
        if item.in_official_file:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "This proposal was made outside Makna in the official file. Reject it first; it "
                "then becomes a draft that can be edited.",
            )
        proposal = self._parse(ref, yaml_text)
        domain = self._domain_of(proposal, state)
        if domain != item.domain:
            raise invalid_input("domain", "a change cannot move the object to another domain")
        if item.official is None:
            created = approval.may_create_new(caller, proposal)
            if not created:
                raise _denied(created)  # the owner team of a new object holds on every edit
        version = item.official.version if item.official is not None else None
        text = to_yaml(_as_proposal(proposal, "draft", version)).encode("utf-8")
        assert item.proposal_path is not None
        if text == item.proposal_bytes:
            raise ServiceError(LifecycleErrorCode.CONFLICT, "The proposal is already exactly this.")
        from_status = item.proposal.status if item.proposal is not None else None
        return _Plan({item.proposal_path: text}, from_status, "draft", version)

    def _plan_proposal_action(
        self,
        caller: Caller,
        user: accounts.User,
        action: str,
        ref: ObjectRef,
        item: ObjectState,
        state: RepoState,
        expected_commit: str | None = None,
    ) -> _Plan:
        if item.proposal_source is None:
            raise ServiceError(LifecycleErrorCode.CONFLICT, "This object has no proposal.")
        assert item.proposal_path is not None
        official, proposal = item.official, item.proposal
        version = official.version if official is not None else None
        status = proposal.status if proposal is not None else None
        if action == WITHDRAW:
            decision = approval.may_edit(caller, user.email, item.authors)
            if not decision:
                raise _denied(decision)
            files: dict[str, bytes | None] = {}
            if item.proposal_source == item.proposal_path:
                files[item.proposal_path] = None
            files.update(_restore_official(item))
            return _Plan(files, status, "withdrawn", version)
        if proposal is None:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "The proposal cannot be read as a definition; withdraw it or replace it.",
            )
        if action == SUBMIT:
            decision = approval.may_edit(caller, user.email, item.authors)
            if not decision:
                raise _denied(decision)
            if status != "draft":
                raise ServiceError(LifecycleErrorCode.CONFLICT, "Only a draft can be submitted.")
            if item.errors:
                raise _issues_error(
                    "The proposal has definition errors, so it cannot be submitted.",
                    list(item.errors),
                )
            text = to_yaml(_as_proposal(proposal, "in_review", version)).encode("utf-8")
            return _Plan({item.proposal_path: text}, "draft", "in_review", version)
        decision = approval.may_decide(
            caller, user.email, official, proposal, item.authors, self._settings, action
        )
        if not decision:
            raise _denied(decision)
        if status != "in_review":
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, "Only a proposal that is in review can be decided."
            )
        if expected_commit is not None and item.proposal_commit != expected_commit:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "The proposal changed after you looked at it. Review it again before deciding.",
            )
        if action == REJECT:
            draft = to_yaml(_as_proposal(proposal, "draft", version)).encode("utf-8")
            reject_files: dict[str, bytes | None] = {item.proposal_path: draft}
            reject_files.update(_restore_official(item))
            return _Plan(reject_files, "in_review", "draft", version)
        return self._plan_approve(item, ref, state, proposal)

    def _plan_approve(
        self, item: ObjectState, ref: ObjectRef, state: RepoState, proposal: AnyDefinition
    ) -> _Plan:
        official = item.official
        if official is not None and official.status == "deprecated":
            raise ServiceError(
                LifecycleErrorCode.CONFLICT,
                "The object is deprecated, so a change cannot be approved.",
            )
        if item.errors:
            raise _issues_error(
                "The proposal has definition errors, so it cannot be approved.", list(item.errors)
            )
        with self._engine.connect() as connection:
            open_issues = self._proposal_reference_issues(connection, state, item, proposal)
        if open_issues:
            raise ServiceError(
                LifecycleErrorCode.OPEN_REFERENCE_ISSUE,
                "With the catalog as it is now, the proposed definition would have a reference "
                "issue, so it cannot be approved. Change the definition or the catalog.",
                {"issues": open_issues[:MAX_ISSUES_SHOWN]},
            )
        version = (official.version or 1) + 1 if official is not None else 1
        approved = to_yaml(with_state(proposal, status="approved", version=version)).encode("utf-8")
        target = item.official_path or layout.object_path(ref, item.domain, proposal=False)
        problems = _gate_after(state, {target: approved})
        if problems:
            raise _issues_error(
                GATE_MESSAGE,
                problems,
            )
        files: dict[str, bytes | None] = {target: approved}
        if item.proposal_source is not None and item.proposal_source != target:
            files[item.proposal_source] = None  # the proposal file, or a stray copy outside Makna
        return _Plan(files, "in_review", "approved", version)

    def _plan_deprecate(
        self,
        caller: Caller,
        user: accounts.User,
        ref: ObjectRef,
        item: ObjectState,
        state: RepoState,
        replaced_by: str | None,
        reason: str,
    ) -> _Plan:
        official = item.official
        if official is None or item.official_path is None:
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, "Only an approved object can be deprecated."
            )
        decision = approval.may_decide(
            caller, user.email, official, None, (), self._settings, DEPRECATE
        )
        if not decision:
            raise _denied(decision)
        if official.status != "approved":
            raise ServiceError(
                LifecycleErrorCode.CONFLICT, "Only an approved object can be deprecated."
            )
        if replaced_by is not None:
            target = layout.parse_object_id(replaced_by) if isinstance(replaced_by, str) else None
            found = state.objects.get(target.id) if target is not None else None
            if (
                target is None
                or target.kind != ref.kind
                or target.id == ref.id
                or found is None
                or found.official is None
                or found.official.status != "approved"
            ):
                raise invalid_input(
                    "replaced_by",
                    "an existing approved object of the same kind, other than this one",
                )
        deprecation = (
            Deprecation(reason=reason)
            if replaced_by is None
            else Deprecation(reason=reason, replaced_by=replaced_by)
        )
        text = to_yaml(
            with_state(
                official,
                status="deprecated",
                version=official.version or 1,
                deprecation=deprecation,
            )
        ).encode("utf-8")
        problems = _gate_after(state, {item.official_path: text})
        if problems:
            raise _issues_error(
                GATE_MESSAGE,
                problems,
            )
        return _Plan({item.official_path: text}, "approved", "deprecated", official.version)

    # --- steps 2 to 4 --------------------------------------------------------------------

    def _write_intent(
        self, user: accounts.User, action: str, object_id: str, reason: str, plan: _Plan
    ) -> int:
        files = {p: (sha256(d) if d is not None else None) for p, d in plan.files.items()}
        try:
            with self._engine.begin() as connection:
                return int(
                    connection.execute(
                        insert(audit_event)
                        .values(
                            actor_user_id=user.id,
                            actor_label=user.email,
                            action=action,
                            object_id=object_id,
                            from_status=plan.from_status,
                            to_status=plan.to_status,
                            reason=reason,
                            commit_sha=None,
                            files=files,
                        )
                        .returning(audit_event.c.id)
                    ).scalar_one()
                )
        except SQLAlchemyError as error:
            logger.error("lifecycle: cannot record the intent (%s)", type(error).__name__)
            raise ServiceError(
                ServiceErrorCode.INTERNAL, "The change could not be recorded."
            ) from None

    def _fail_intent(self, intent_id: int) -> None:
        """Mark an attempt that made no commit as failed. The row is kept: it is a trace."""
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    update(audit_event)
                    .where(audit_event.c.id == intent_id, audit_event.c.commit_sha.is_(None))
                    .values(failed_at=func.now())
                )
        except SQLAlchemyError as error:
            logger.error("lifecycle: cannot mark an intent failed (%s)", type(error).__name__)

    def _commit(
        self,
        repo: GitRepo,
        lock: store.HeldLock,
        user: accounts.User,
        action: str,
        object_id: str,
        reason: str,
        plan: _Plan,
        intent_id: int,
        state: RepoState,
    ) -> CommitResult:
        """Step 3: the commit, built from the checked bytes with plumbing, on the `HEAD` that was
        read. The lock is checked first; if it is lost, nothing is committed."""
        try:
            lock.check()
        except ServiceError:
            self._fail_intent(intent_id)
            raise
        try:
            made = repo.commit_files(
                _message(action, object_id, reason, user.id, plan.version),
                user.display_name,
                user.email,
                plan.files,
                state.head,
            )
        except (RepoError, ValueError) as error:
            self._fail_intent(intent_id)
            logger.error("lifecycle: git failed (%s)", type(error).__name__)
            if isinstance(error, RepoError) and error.subcommand == "update-ref":
                raise ServiceError(LifecycleErrorCode.CONFLICT, REPOSITORY_MOVED) from None
            raise ServiceError(
                ServiceErrorCode.INTERNAL, "The change could not be saved, so nothing was changed."
            ) from None
        self._mirror(repo, made, plan.files)
        return made

    @staticmethod
    def _mirror(repo: GitRepo, made: CommitResult, files: Mapping[str, bytes | None]) -> None:
        """Bring the working tree and the repository's own index in line with the commit, so that
        `git status` is quiet. Best effort: the working tree is not authoritative, nothing reads
        it, and a path with a link in it is skipped (never written through)."""
        try:
            repo.index_set(made.blobs)
        except (RepoError, ValueError):
            logger.warning("lifecycle: the repository index could not be updated")
        for path, data in files.items():
            try:
                if data is None:
                    _remove_file(repo.path, path)
                else:
                    _write_file(repo.path, path, data)
            except OSError:
                logger.warning("lifecycle: a file of the working tree was not updated")

    def _finish(
        self, repo: GitRepo, lock: store.HeldLock, intent_id: int, made: CommitResult
    ) -> None:
        """Step 4 under the lock the caller holds: the same code as `makna index sync`, told which
        commit is this action's own."""
        try:
            lock.check()
            state, _ = store.sync(
                self._engine,
                self._provider,
                repo,
                derive=self._derive,
                locked=True,
                claimed={intent_id: made.sha},
            )
        except Exception as error:
            logger.error("lifecycle: committed but not indexed (%s)", type(error).__name__)
            raise ServiceError(ServiceErrorCode.INTERNAL, COMMITTED_NOT_INDEXED) from None
        if state.matched.get(intent_id) != made.sha:
            logger.error("lifecycle: the commit does not match its intent")
            raise ServiceError(ServiceErrorCode.INTERNAL, COMMITTED_NOT_INDEXED)


def _check_size(yaml_text: object) -> None:
    if not isinstance(yaml_text, str):
        raise invalid_input("yaml", "the definition as YAML text")
    if len(yaml_text.encode("utf-8", "replace")) > MAX_YAML_BYTES:
        raise invalid_input("yaml", f"at most {MAX_YAML_BYTES // 1024} KiB", maximum=MAX_YAML_BYTES)


def _as_proposal(definition: AnyDefinition, status: str, version: int | None) -> AnyDefinition:
    """The proposal as written to its file: the status the action sets, and the version of the
    official object it changes (left out for a new object); no deprecation."""
    copy = definition.model_copy(update={"status": status})
    fields = set(copy.model_fields_set)
    copy.version = version
    copy.deprecation = None
    fields.discard("deprecation")
    fields.add("version") if version is not None else fields.discard("version")
    copy.__pydantic_fields_set__ = fields
    return copy


def _restore_official(item: ObjectState) -> dict[str, bytes | None]:
    """When an outside commit left the proposal's content in a file other than the proposal file,
    the files that put things back: the official file as the application last wrote it, or no file
    where there is no official one (a new object, or a stray copy elsewhere)."""
    source = item.proposal_source
    if source is None or not item.in_official_file:
        return {}
    if source == item.official_path and item.official_bytes is not None:
        return {source: item.official_bytes}
    return {source: None}


def _gate_after(state: RepoState, replaced: Mapping[str, bytes]) -> list[Issue]:
    """The build gate on the official files with `replaced` (path -> new bytes) put in place."""
    files = [f for f in state.official_files if f.path not in replaced]
    issues: list[Issue] = list(state.official_load_issues)
    for path, data in replaced.items():
        file, found = load_text(path, data.decode("utf-8"))
        issues.extend(found)
        files.append(file)
    return validate_for_build(DefinitionSet(Path("."), files, issues))


# --- the seed ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedOutcome:
    seeded: bool
    message: str


def seed_repository(engine: Engine, repo_path: Path, source: Path) -> SeedOutcome:
    """`init`'s step: when `repo_path` is missing or empty, make it a git repository and commit
    the definitions of `source` (a directory, never written to) as one application action `seed`.
    A repository that already has commits, or a directory that holds anything else, is left
    alone, so a second `init` changes nothing.

    The commit is recorded as an open intent; the index step that follows (`sync`) completes it,
    and so does the next start of the application if `init` stops in between. Raises
    `store.StoreError` when `source` fails the checks of `makna validate`.
    """
    repo = GitRepo(repo_path)
    if repo_path.exists() and not repo_path.is_dir():
        return SeedOutcome(False, f"{repo_path} is not a directory; left alone")
    dot_git = repo_path / ".git"
    if os.path.lexists(dot_git) and (dot_git.is_symlink() or not dot_git.is_dir()):
        # A planted `.git` file or link would send the commit into another repository.
        raise store.StoreError("the definitions path has a .git that is not a directory of its own")
    if repo_path.is_dir():
        entries = [p for p in repo_path.iterdir() if p.name != ".git"]
        try:
            has_commits = dot_git.is_dir() and repo.rev_parse("HEAD") is not None
        except RepoError:
            raise store.StoreError("the definitions repository cannot be read") from None
        if entries or has_commits:
            return SeedOutcome(False, "the definitions repository already has content; left alone")
    loaded = load_definitions(source)
    problems = validate_for_build(loaded)
    if problems:
        raise store.StoreError(
            f"the definitions in {source} have {len(problems)} problem(s), so nothing was seeded",
            tuple(problems),
        )
    files: dict[str, bytes] = {}
    for file in loaded.files:
        if layout.parse_path(file.path) is not None:
            files[file.path] = (source / file.path).read_bytes()
    if not files:
        raise store.StoreError(f"{source} holds no definition files; nothing was seeded")
    reason = "the first definitions of the repository"
    with store.advisory_lock(engine) as lock:
        try:
            if not dot_git.is_dir():
                repo.init()
            if repository_mode(repo_path, configured=True) is not RepoMode.REPOSITORY:
                raise store.StoreError("the definitions path is not a repository of its own")
        except (RepoError, RepoUnusable):
            raise store.StoreError("the definitions repository could not be created") from None
        with engine.begin() as connection:
            intent_id = connection.execute(
                insert(audit_event)
                .values(
                    actor_label=COMMITTER_NAME,
                    action=store.SEED_ACTION,
                    object_id=store.SEED_OBJECT,
                    to_status="approved",
                    reason=reason,
                    commit_sha=None,
                    files={p: sha256(d) for p, d in files.items()},
                )
                .returning(audit_event.c.id)
            ).scalar_one()
        try:
            lock.check()
            made = repo.commit_files(
                _message(store.SEED_ACTION, store.SEED_OBJECT, reason, None, 1),
                COMMITTER_NAME,
                COMMITTER_EMAIL,
                files,
                None,
            )
        except (RepoError, ServiceError, ValueError):
            with engine.begin() as connection:
                connection.execute(
                    update(audit_event)
                    .where(audit_event.c.id == intent_id)
                    .values(failed_at=func.now())
                )
            raise store.StoreError("the definitions could not be committed") from None
        Lifecycle._mirror(repo, made, files)
    return SeedOutcome(True, f"committed {len(files)} definition file(s) as the action seed")


__all__ = ["ACTIONS", "ActionResult", "Lifecycle", "SeedOutcome", "check_reason", "seed_repository"]
