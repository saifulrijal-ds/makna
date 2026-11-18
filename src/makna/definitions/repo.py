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
- `-c core.hooksPath=/dev/null`, and `--no-verify` on `commit`: no hook runs, so a script planted
  in `.git/hooks` stays inert;
- `-c commit.gpgsign=false`, `-c core.fsmonitor=false`, `-c gc.auto=0`, `-c core.autocrlf=false`,
  `-c core.quotePath=false`: no signing program, no monitor daemon, no background gc, bytes in
  and out are the same bytes.

Every call names the repository with `--git-dir` and `--work-tree`, so git never searches parent
directories for another repository. The repository's own `.git/config` is trusted: whoever can
write it can already run code as the application. What a raw commit can carry, files, is only
ever read as data (`cat-file`), never checked out or run through a filter.

The author of a commit is set with `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL`, the committer is
`Makna` at a fixed address; `-c user.*` is not used.

**Errors.** A failed command raises one `RepoError` that carries the subcommand and the exit code.
Git's standard error is never kept on the exception, because it can hold file content and paths;
it is written to the application log at debug level only.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 60
COMMITTER_NAME = "Makna"
COMMITTER_EMAIL = "makna@localhost"
BRANCH = "main"
_SHA = re.compile(r"[0-9a-f]{40}")
_REGULAR_MODES = ("100644", "100755")

_FIXED_CONFIG = (
    "core.hooksPath=/dev/null",
    "core.attributesFile=/dev/null",
    "core.fsmonitor=false",
    "commit.gpgsign=false",
    "gc.auto=0",
    "core.autocrlf=false",
    "core.quotePath=false",
)


class RepoError(Exception):
    """A git command failed. Carries the subcommand and the exit code, never git's output."""

    def __init__(self, subcommand: str, returncode: int | None) -> None:
        outcome = "timed out" if returncode is None else f"exit {returncode}"
        super().__init__(f"git {subcommand} failed ({outcome})")
        self.subcommand = subcommand
        self.returncode = returncode


@dataclass(frozen=True)
class Commit:
    """One commit of the first-parent history, and the files it changed against its first parent.

    `changes` holds `(status, path)` with the status letter git prints: `A`, `M`, `D`, `T`.
    """

    sha: str
    author_name: str
    author_email: str
    parents: tuple[str, ...]
    changes: tuple[tuple[str, str], ...]


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


def _check_email(email: str) -> str:
    if (
        not email
        or len(email) > 254
        or any(unicodedata.category(c)[0] in "CZ" for c in email)
        or any(c in "<>" for c in email)
    ):
        raise ValueError("not a usable author email")
    return email


def is_repository_root(path: Path) -> bool:
    """True when `path` is the top level of a git repository (its own `.git` directory), as git
    itself reports it. A directory inside another repository is not: it is a plain directory."""
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


class GitRepo:
    """One definitions repository: a working tree with a `.git` directory at `path`."""

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
        if discover:
            command += ["-C", str(directory)]
        else:
            command += [f"--git-dir={directory / '.git'}", f"--work-tree={directory}"]
        command += args
        try:
            done = subprocess.run(  # noqa: S603 - an argument list, never a shell
                command,
                input=stdin,
                capture_output=True,
                timeout=timeout,
                env=GitRepo._environment(extra_env),
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
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--no-renames",
                "-r",
                "--name-status",
                "-z",
                sha,
            ]
        )
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
        return self.read_blobs([(sha, path)])[(sha, path)]

    def read_blobs(self, wanted: list[tuple[str, str]]) -> dict[tuple[str, str], bytes | None]:
        """The bytes of many `(sha, path)` pairs with one `git cat-file --batch`. A pair that
        names no file, or a path with a line break in it, maps to None."""
        result: dict[tuple[str, str], bytes | None] = {}
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
        out = self._git(["cat-file", "--batch"], stdin=request)
        position = 0
        for pair in asked:
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

    def add(self, paths: list[str]) -> None:
        self._git(["add", "--force", *self._pathspecs(paths)])

    def rm(self, paths: list[str]) -> None:
        """Delete the files from the working tree and stage the deletion."""
        self._git(["rm", "-q", "--force", *self._pathspecs(paths)])

    def commit(self, message: str, author_name: str, author_email: str, paths: list[str]) -> str:
        """Commit exactly `paths` (staged additions and deletions of those paths only; anything
        else staged is left staged) and return the new sha."""
        env = {
            "GIT_AUTHOR_NAME": clean_display_name(author_name),
            "GIT_AUTHOR_EMAIL": _check_email(author_email),
            "GIT_COMMITTER_NAME": COMMITTER_NAME,
            "GIT_COMMITTER_EMAIL": COMMITTER_EMAIL,
        }
        self._git(
            ["commit", "-q", "--only", "--no-verify", "--no-gpg-sign", "--cleanup=verbatim",
             "-F", "-", *self._pathspecs(paths)],
            stdin=message.encode("utf-8"),
            extra_env=env,
        )  # fmt: skip
        sha = self.rev_parse("HEAD")
        if sha is None:
            raise RepoError("commit", None)
        return sha

    def discard(self, paths: list[str]) -> None:
        """Undo an uncommitted write of `paths`: unstage them and put back what `HEAD` has (or
        remove a file `HEAD` does not have). Best effort, used after a failed commit."""
        head = self.rev_parse("HEAD")
        for path in paths:
            try:
                if head is not None and self.show(head, path) is not None:
                    self._git(["reset", "-q", "HEAD", *self._pathspecs([path])])
                    self._git(["checkout", "-q", "HEAD", *self._pathspecs([path])])
                else:
                    self._git(["rm", "-q", "--force", "--cached", "--ignore-unmatch",
                               *self._pathspecs([path])])  # fmt: skip
                    target = self.path / path
                    if target.is_file() and not target.is_symlink():
                        target.unlink()
            except (RepoError, OSError):
                logger.warning("could not undo an uncommitted file (%s)", type(self).__name__)

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
        name = tokens[index + 1].decode("utf-8", "replace")
        email = tokens[index + 2].decode("utf-8", "replace")
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
