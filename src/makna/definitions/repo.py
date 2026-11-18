"""The git command line, behind one small class (ARCHITECTURE section 10).

`GitRepo` is the only code in Makna that starts `git`. It calls the command line, never a shell:
every call is `subprocess.run` with an argument list, a timeout, and `--` before every pathspec,
so a file name cannot become an option.

**What is isolated, and why.** Git reads configuration, hooks, and environment variables from the
machine, and any of them can run a program or change what a command does. Every call therefore
starts from an empty environment (nothing is inherited) and sets only:

- `PATH` (to find `git`), `LC_ALL=C` (messages and sorting do not depend on the locale);
- `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_GLOBAL=/dev/null`: no system or user configuration. There
  is no `HOME`, so git finds no other per-user file either;
- `GIT_ATTR_NOSYSTEM=1`: no system attributes file; `-c core.attributesFile=/dev/null`: no user one;
- `GIT_TERMINAL_PROMPT=0` and `GIT_EDITOR=true`: git never waits for a person; `--no-pager`;
- `GIT_OPTIONAL_LOCKS=0`: a read does not write the index;
- `-c core.hooksPath=/dev/null`: no hook runs, so a script planted in `.git/hooks` stays inert;
- `GIT_NO_REPLACE_OBJECTS=1` and `-c core.commitGraph=false`: git shows an object as it is, never as
  a `refs/replace/*` ref (which a push can add) or a commit-graph file says it should be;
- `GIT_ATTR_SOURCE=<the empty tree>`: no attributes are read from any tree;
- `-c commit.gpgsign=false`, `-c core.fsmonitor=false`, `-c gc.auto=0`, `-c core.autocrlf=false`,
  `-c core.quotePath=false`: no signing program, no monitor daemon, no background gc, bytes in
  and out are the same bytes.

Every call names the repository with `--git-dir` and names as its **work tree an empty directory
of our own** (and runs there), so git never searches parent directories for another repository,
never sees the real working tree, its `.gitattributes`, or its `.gitmodules`, and has no file to
run a `clean`, `smudge`, or `textconv` filter on, whatever the git version and whatever the
repository's attributes or configuration say. **The working tree is not authoritative and git
never reads or writes it for Makna.** A commit is built from the bytes the caller
checked, with plumbing and a private index file (`hash-object --no-filters`, `read-tree`,
`update-index --index-info`, `write-tree`, `commit-tree`, then `update-ref` with the old value
that was read), so no `.gitattributes` or `filter` setting can change what is committed, no file is
read from or written to the working tree, and no `.gitmodules` of the working tree is read. The
history reads use `--ignore-submodules=all`.

What this does **not** cover, and what covers it. Git can show other content under the sha of a
commit the application made: a replace ref (`refs/replace/*`, which a push can add; ignored here),
a graft or a shallow file (refused by `repository_mode`), a commit-graph file (ignored), or an
object file rewritten in place (git does not check the hash on read). The last one, and any other,
is caught by the audit log: `makna.services.store` compares what it reads from a trusted commit
with the files and SHA-256 hashes recorded for that commit in `audit_event`, and refuses a
mismatch. The repository's own `.git/config` is trusted: whoever can write it can already run code
as the application (and so can whoever can write the repository directory; see ARCHITECTURE
section 10).

The author of a commit is set with `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL`, the committer is
`Makna` at a fixed address; `-c user.*` is not used.

**Errors.** A failed command raises one `RepoError` that carries the subcommand and the exit code.
Git's standard error is never kept on the exception, because it can hold file content and paths;
it is written to the application log at debug level only. A directory that cannot be used as a
definitions repository is a `RepoUnusable` with a fixed reason text.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
COMMITTER_NAME = "Makna"
COMMITTER_EMAIL = "makna@localhost"
BRANCH = "main"
_SHA = re.compile(r"[0-9a-f]{40}")
_REGULAR_MODES = ("100644", "100755")

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_FIXED_CONFIG = (
    "core.commitGraph=false",
    "core.hooksPath=/dev/null",
    "core.attributesFile=/dev/null",
    "core.fsmonitor=false",
    "commit.gpgsign=false",
    "gc.auto=0",
    "core.autocrlf=false",
    "core.quotePath=false",
)


_EMPTY_TREE: Path | None = None


def _empty_work_tree() -> Path:
    """An empty directory of our own, the work tree every command is given (see the module
    docstring). Made once per process and removed at exit."""
    global _EMPTY_TREE
    if _EMPTY_TREE is None or not _EMPTY_TREE.is_dir():
        created = Path(tempfile.mkdtemp(prefix="makna-empty-tree-"))
        atexit.register(shutil.rmtree, created, True)
        _EMPTY_TREE = created
    return _EMPTY_TREE


class RepoError(Exception):
    """A git command failed. Carries the subcommand and the exit code, never git's output."""

    def __init__(self, subcommand: str, returncode: int | None) -> None:
        outcome = "timed out" if returncode is None else f"exit {returncode}"
        super().__init__(f"git {subcommand} failed ({outcome})")
        self.subcommand = subcommand
        self.returncode = returncode


class RepoUnusable(Exception):
    """A configured definitions path is neither a repository of its own nor a plain directory
    inside another repository. `reason` is a short code; the message is fixed text that holds no
    path and no git output."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class Commit:
    """One commit of the first-parent history, and the files it changed against its first parent.

    `changes` holds `(status, path)` with the status letter git prints: `A`, `M`, `D`, `T`.
    Names and emails of authors are cleaned of control and format characters when read.
    """

    sha: str
    author_name: str
    author_email: str
    parents: tuple[str, ...]
    changes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CommitResult:
    """A commit made by `GitRepo.commit_files` and the blob of each path it wrote (None for a
    deleted path)."""

    sha: str
    blobs: Mapping[str, str | None]


@dataclass(frozen=True)
class TooLarge:
    """A blob that is larger than the limit asked for: it was not read."""

    size: int


def clean_display_name(name: str) -> str:
    """A name safe for a commit author: no control or format character, no `<` or `>`, one line,
    at most 100 characters. A name that is empty afterwards becomes `Makna user`."""
    kept = "".join(
        " " if unicodedata.category(c) in ("Zl", "Zp", "Cc") else c
        for c in name
        if not unicodedata.category(c) == "Cf" and c not in "<>"
    )
    kept = " ".join(kept.split())[:100].strip()
    return kept or "Makna user"


def clean_email(email: str) -> str:
    """An email read from a commit that someone else made, made safe to store and show: control,
    format (bidi), separator, and `<>` characters removed, at most 254 characters."""
    return "".join(c for c in email if unicodedata.category(c)[0] not in "CZ" and c not in "<>")[
        :254
    ]


def _check_email(email: str) -> str:
    if (
        not email
        or len(email) > 254
        or any(unicodedata.category(c)[0] in "CZ" for c in email)
        or any(c in "<>" for c in email)
    ):
        raise ValueError("not a usable author email")
    return email


def altered_history(git_dir: Path) -> str | None:
    """Why the history of the repository whose `.git` is `git_dir` cannot be believed, or None.
    A shallow file or grafts file changes which parents a commit seems to have, so a commit the
    application made can seem to contain other things; the application refuses such a repository."""
    if os.path.lexists(git_dir / "shallow"):
        return "the repository is shallow (a .git/shallow file changes what its commits contain)"
    if os.path.lexists(git_dir / "info" / "grafts"):
        return (
            "the repository has grafts (a .git/info/grafts file changes what its commits contain)"
        )
    return None


class RepoMode(StrEnum):
    PLAIN = "plain"
    """A directory that is read as it is and never written (no git repository of its own)."""
    REPOSITORY = "repository"
    """The top level of a git repository: the application writes it by committing."""


def is_repository_root(path: Path) -> bool:
    """True when `path` is the top level of a git repository (its own `.git` directory), as git
    itself reports it. A directory inside another repository is not."""
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    dot_git = resolved / ".git"
    if dot_git.is_symlink() or not dot_git.is_dir():
        return False
    try:
        out = GitRepo._run_in(resolved, ["rev-parse", "--show-toplevel"], discover=True)
    except RepoError:
        return False
    return Path(out.decode("utf-8", "replace").strip()).resolve() == resolved


def repository_mode(path: Path, *, configured: bool) -> RepoMode:
    """How the definitions at `path` are to be treated. Raises `RepoUnusable` when a repository
    that should be there is broken: a broken repository is never read as a plain directory.

    - A `.git` entry at `path` means a repository. It must be a real directory and git must
      report `path` as the top level; otherwise (a link, a file, a bare or re-rooted repository,
      another owner, git missing) it is an error, whether `path` is configured or not.
    - Without a `.git` entry, a path that was **not** configured (`makna ... --definitions DIR`)
      is a plain directory, as it always was, and git is not asked.
    - Without a `.git` entry, a **configured** path (`MAKNA_DEFINITIONS_REPO`) is a plain
      directory only if it lies inside another git repository (the source tree's sample
      definitions); anywhere else it is an error, because a repository that lost its `.git` must
      not be served from its working tree.
    """
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise RepoUnusable("missing", "the definitions directory does not exist") from None
    dot_git = resolved / ".git"
    if os.path.lexists(dot_git):
        if dot_git.is_symlink() or not dot_git.is_dir():
            raise RepoUnusable(
                "git_entry_not_a_directory",
                "the definitions directory has a .git that is not a directory of its own",
            )
        if is_repository_root(resolved):
            why = altered_history(dot_git)
            if why is not None:
                raise RepoUnusable("altered_history", why)
            return RepoMode.REPOSITORY
        raise RepoUnusable(
            "not_a_usable_repository",
            "git does not accept the definitions repository (a bare or re-rooted repository, "
            "another owner, or git is not available)",
        )
    if not configured:
        return RepoMode.PLAIN
    try:
        out = GitRepo._run_in(resolved, ["rev-parse", "--show-toplevel"], discover=True)
    except RepoError:
        raise RepoUnusable(
            "not_a_repository",
            "MAKNA_DEFINITIONS_REPO is not a git repository and not inside one; if the "
            "repository lost its .git, restore it",
        ) from None
    if Path(out.decode("utf-8", "replace").strip()).resolve() == resolved:
        raise RepoUnusable("not_a_usable_repository", "git does not accept the repository")
    return RepoMode.PLAIN


class GitRepo:
    """One definitions repository: a directory with a `.git` directory at `path`."""

    def __init__(self, path: Path | str, *, timeout: int = TIMEOUT_SECONDS) -> None:
        self.path = Path(path).resolve()
        self.timeout = timeout

    # --- running git -------------------------------------------------------------------

    @staticmethod
    def _environment(extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ATTR_SOURCE": EMPTY_TREE,
        }
        env.update(extra or {})
        return env

    @staticmethod
    def _run_in(
        directory: Path,
        args: list[str],
        *,
        discover: bool = False,
        stdin: bytes | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: int = TIMEOUT_SECONDS,
    ) -> bytes:
        command = ["git", "--no-pager"]
        for item in _FIXED_CONFIG:
            command += ["-c", item]
        work = directory
        if discover:
            command += ["-C", str(directory)]
        else:
            work = _empty_work_tree()
            command += [f"--git-dir={directory / '.git'}", f"--work-tree={work}"]
        command += args
        try:
            done = subprocess.run(  # noqa: S603 - an argument list, never a shell
                command,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                env=GitRepo._environment(extra_env),
                cwd=work,
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise RepoError(args[0], None) from None
        if done.returncode != 0:
            # Debug only: standard error can hold file content and paths.
            logger.debug("git %s failed: %s", args[0], done.stderr[:500].decode("utf-8", "replace"))
            raise RepoError(args[0], done.returncode)
        return done.stdout

    def _git(
        self,
        args: list[str],
        *,
        stdin: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> bytes:
        return self._run_in(self.path, args, stdin=stdin, extra_env=extra_env, timeout=self.timeout)

    @staticmethod
    def _pathspecs(paths: list[str]) -> list[str]:
        for path in paths:
            if not path or path.startswith(("/", "-")) or "\0" in path or ".." in path.split("/"):
                raise ValueError("not a path inside the repository")
        return ["--", *paths]

    # --- the repository ------------------------------------------------------------------

    def init(self) -> None:
        """`git init` in an existing, empty directory, on the branch `main`."""
        self.path.mkdir(parents=True, exist_ok=True)
        # `init` has no repository yet, so it cannot use --git-dir.
        command = ["git", "--no-pager", "-C", str(self.path), "init", "-q", "-b", BRANCH]
        try:
            done = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                timeout=self.timeout,
                env=self._environment(),
                check=False,
                shell=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise RepoError("init", None) from None
        if done.returncode != 0:
            raise RepoError("init", done.returncode)

    def rev_parse(self, revision: str = "HEAD") -> str | None:
        """The sha `revision` names, or None when it names nothing (an empty repository)."""
        if revision.startswith("-"):
            raise ValueError("not a revision")
        try:
            out = self._git(["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
        except RepoError as error:
            if error.returncode == 1:
                return None
            raise
        sha = out.decode("ascii", "replace").strip()
        return sha if _SHA.fullmatch(sha) else None

    def is_ancestor(self, ancestor: str, descendant: str = "HEAD") -> bool:
        """True when `ancestor` is `descendant` or one of its ancestors. A commit that does not
        exist in the repository (history was rewritten or the repository replaced) is not."""
        self._check_sha(ancestor)
        if descendant != "HEAD":
            self._check_sha(descendant)
        try:
            self._git(["merge-base", "--is-ancestor", ancestor, descendant])
        except RepoError as error:
            if error.returncode in (1, 128):
                return False
            raise
        return True

    def log(self) -> list[Commit]:
        """Every commit of the first-parent history of `HEAD`, oldest first, with the files each
        changed against its first parent (a merge shows what it brought in). Empty when the
        repository has no commit."""
        if self.rev_parse("HEAD") is None:
            return []
        raw = self._git(
            [
                "log",
                "--first-parent",
                "--reverse",
                "--no-renames",
                "--ignore-submodules=all",
                "--name-status",
                "-z",
                "--format=%x01%H%x00%an%x00%ae%x00%P",
            ]
        )
        return _parse_log(raw)

    def diff_names(self, sha: str) -> list[tuple[str, str]]:
        """`(status, path)` of the files `sha` changed against its parent (all of them for a root
        commit). Meant for the commits Makna makes, which have at most one parent."""
        self._check_sha(sha)
        raw = self._git(
            [
                "diff-tree", "--root", "--no-commit-id", "--no-renames", "--ignore-submodules=all",
                "-r", "--name-status", "-z", sha,
            ]
        )  # fmt: skip
        return _parse_name_status(raw.split(b"\0"))

    def regular_files(self, sha: str) -> list[str]:
        """The paths of the regular files in the tree of `sha` (symbolic links and submodules are
        left out: Makna reads files only)."""
        self._check_sha(sha)
        raw = self._git(["ls-tree", "-r", "-z", "--full-tree", sha])
        paths: list[str] = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            meta, _, name = entry.partition(b"\t")
            fields = meta.split()
            if len(fields) == 3 and fields[0].decode("ascii", "replace") in _REGULAR_MODES:
                paths.append(name.decode("utf-8", "surrogateescape"))
        return paths

    def show(self, sha: str, path: str) -> bytes | None:
        """The bytes of `path` as of commit `sha`, or None when the commit has no such file."""
        found = self.read_blobs([(sha, path)])[(sha, path)]
        return None if isinstance(found, TooLarge) else found

    def read_blobs(
        self, wanted: list[tuple[str, str]], max_size: int | None = None
    ) -> dict[tuple[str, str], bytes | None | TooLarge]:
        """The bytes of many `(sha, path)` pairs, read with `git cat-file`. A pair that names no
        file (or a path with a line break in it, or an object that is not a blob) maps to None. A
        blob larger than `max_size` is not read and maps to `TooLarge`. Sizes are asked first
        (`--batch-check`), so only blobs of an acceptable size are ever read into memory."""
        result: dict[tuple[str, str], bytes | None | TooLarge] = {}
        asked: list[tuple[str, str]] = []
        for sha, path in dict.fromkeys(wanted):
            self._check_sha(sha)
            if "\n" in path or "\0" in path:
                result[(sha, path)] = None
            else:
                asked.append((sha, path))
        if not asked:
            return result
        request = "".join(f"{sha}:{path}\n" for sha, path in asked).encode(
            "utf-8", "surrogateescape"
        )
        checked = self._git(["cat-file", "--batch-check"], stdin=request).split(b"\n")
        readable: list[tuple[str, str]] = []
        for pair, line in zip(asked, checked, strict=False):
            fields = line.split()
            if len(fields) == 3 and fields[1] == b"blob":
                size = int(fields[2])
                if max_size is not None and size > max_size:
                    result[pair] = TooLarge(size)
                else:
                    readable.append(pair)
            else:
                result[pair] = None
        if readable:
            wanted_again = "".join(f"{sha}:{path}\n" for sha, path in readable).encode(
                "utf-8", "surrogateescape"
            )
            out = self._git(["cat-file", "--batch"], stdin=wanted_again)
            position = 0
            for pair in readable:
                end = out.index(b"\n", position)
                header = out[position:end].split()
                position = end + 1
                if len(header) == 3 and header[1] == b"blob":
                    size = int(header[2])
                    result[pair] = out[position : position + size]
                    position += size + 1
                else:
                    result[pair] = None
        return result

    # --- writing -------------------------------------------------------------------------

    def commit_files(
        self,
        message: str,
        author_name: str,
        author_email: str,
        changes: Mapping[str, bytes | None],
        parent: str | None,
    ) -> CommitResult:
        """Commit `changes` (path -> exact bytes, or None to delete) on top of `parent`, which
        must be what `HEAD` is now, and move `HEAD` to the new commit.

        The tree is built in a private index file in a temporary directory from the bytes given:
        `hash-object -w --no-filters --stdin` for each file, `read-tree` of the parent,
        `update-index --index-info`, `write-tree`, `commit-tree`, and `update-ref HEAD new old`,
        which fails if `HEAD` moved since `parent` was read. The working tree and the repository's
        own index are not read or written, and no attribute or filter applies.
        """
        paths = list(changes)
        self._pathspecs(paths)
        if any("\n" in path for path in paths):
            raise ValueError("not a path inside the repository")
        env = {
            "GIT_AUTHOR_NAME": clean_display_name(author_name),
            "GIT_AUTHOR_EMAIL": _check_email(author_email),
            "GIT_COMMITTER_NAME": COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        }
        if parent is not None:
            self._check_sha(parent)
        with tempfile.TemporaryDirectory(prefix="makna-index-") as scratch:
            private = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
            if parent is not None:
                self._git(["read-tree", parent], extra_env=private)
            else:
                self._git(["read-tree", "--empty"], extra_env=private)
            blobs: dict[str, str | None] = {}
            lines: list[str] = []
            for path, data in changes.items():
                if data is None:
                    blobs[path] = None
                    lines.append(f"0 {'0' * 40}\t{path}\n")
                    continue
                oid = (
                    self._git(["hash-object", "-w", "--no-filters", "-t", "blob", "--stdin"],
                              stdin=data, extra_env=private)
                    .decode("ascii", "replace")
                    .strip()
                )  # fmt: skip
                if not _SHA.fullmatch(oid):
                    raise RepoError("hash-object", None)
                blobs[path] = oid
                lines.append(f"100644 {oid}\t{path}\n")
            self._git(
                ["update-index", "--index-info"],
                stdin="".join(lines).encode("utf-8"),
                extra_env=private,
            )
            tree = self._git(["write-tree"], extra_env=private).decode("ascii", "replace").strip()
            command = ["commit-tree", tree]
            if parent is not None:
                command += ["-p", parent]
            command += ["-F", "-"]
            sha = (
                self._git(command, stdin=message.encode("utf-8"), extra_env={**private, **env})
                .decode("ascii", "replace")
                .strip()
            )
        if not _SHA.fullmatch(sha) or not _SHA.fullmatch(tree):
            raise RepoError("commit-tree", None)
        self._git(["update-ref", "HEAD", sha, parent if parent is not None else "0" * 40])
        return CommitResult(sha, blobs)

    def index_set(self, blobs: Mapping[str, str | None]) -> None:
        """Make the repository's own index (what `git status` compares with) say what the
        commit just made says for these paths. Only `update-index --index-info`: no file is read."""
        self._pathspecs(list(blobs))
        lines = "".join(
            f"0 {'0' * 40}\t{path}\n" if oid is None else f"100644 {oid}\t{path}\n"
            for path, oid in blobs.items()
        )
        self._git(["update-index", "--index-info"], stdin=lines.encode("utf-8"))

    @staticmethod
    def _check_sha(sha: str) -> None:
        if not _SHA.fullmatch(sha):
            raise ValueError("not a commit sha")


def _parse_name_status(tokens: list[bytes]) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    index = 0
    while index + 1 < len(tokens):
        status = tokens[index].decode("ascii", "replace").strip()
        if not status:
            index += 1
            continue
        path = tokens[index + 1].decode("utf-8", "surrogateescape")
        changes.append((status[0], path))
        index += 2
    return changes


def _parse_log(raw: bytes) -> list[Commit]:
    """Parse `git log -z --name-status` with the format `\\x01sha NUL name NUL email NUL parents`.

    Positional, not by splitting on `\\x01`: a name may hold any byte except NUL and a path any
    byte except NUL, so only the header (a `\\x01` and 40 hex digits) tells one commit from
    the next."""
    tokens = raw.split(b"\0")
    commits: list[Commit] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not (
            len(token) == 41
            and token[:1] == b"\x01"
            and _SHA.fullmatch(token[1:].decode("ascii", "replace"))
        ):
            index += 1
            continue
        sha = token[1:].decode("ascii")
        name = clean_display_name(tokens[index + 1].decode("utf-8", "replace"))
        email = clean_email(tokens[index + 2].decode("utf-8", "replace"))
        parents = tuple(tokens[index + 3].decode("ascii", "replace").split())
        index += 4
        changes: list[tuple[str, str]] = []
        while index + 1 < len(tokens):
            status = tokens[index].decode("ascii", "replace").strip()
            if len(status) != 1 or status not in "AMDTUXB":
                break
            changes.append((status, tokens[index + 1].decode("utf-8", "surrogateescape")))
            index += 2
        commits.append(Commit(sha, name, email, parents, tuple(changes)))
    return commits
