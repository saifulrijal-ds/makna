"""WP-18: `makna.definitions.repo.GitRepo`, the layout of paths, and the canonical YAML.

These need the `git` command line (no database): a temporary repository per test.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from makna.definitions import layout, render
from makna.definitions.loader import load_definitions
from makna.definitions.repo import (
    GitRepo,
    RepoError,
    RepoMode,
    RepoUnusable,
    TooLarge,
    clean_display_name,
    clean_email,
    is_repository_root,
    repository_mode,
)
from makna.model.definitions import Deprecation, dump_definition

SAMPLE = Path(__file__).resolve().parents[2] / "semantic" / "definitions"


def make_repo(path: Path) -> GitRepo:
    repo = GitRepo(path)
    repo.init()
    return repo


def commit_files(
    repo: GitRepo, changes: Mapping[str, bytes | None], *, who: str = "ann@example.com"
) -> str:
    return repo.commit_files("edit: test", "Ann", who, changes, repo.rev_parse("HEAD")).sha


def commit_file(repo: GitRepo, name: str, text: str, *, who: str = "ann@example.com") -> str:
    return commit_files(repo, {name: text.encode()}, who=who)


def raw(repo: GitRepo, *args: str, **env: str) -> str:
    """A git command as someone outside Makna runs it, on the working tree."""
    full = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Outside",
        "GIT_AUTHOR_EMAIL": "outside@example.com",
        "GIT_COMMITTER_NAME": "Outside",
        "GIT_COMMITTER_EMAIL": "outside@example.com",
        **env,
    }
    done = subprocess.run(
        ["git", "-C", str(repo.path), *args], capture_output=True, text=True, check=True, env=full
    )
    return done.stdout


def raw_commit(repo: GitRepo, files: Mapping[str, str], message: str = "outside") -> None:
    for name, text in files.items():
        target = repo.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        raw(repo, "add", "--", name)
    raw(repo, "commit", "-q", "-m", message)


def test_a_commit_has_the_user_as_author_and_makna_as_committer(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    sha = commit_file(repo, "a/b.yaml", "x: 1\n")
    log = repo.log()
    assert [c.sha for c in log] == [sha]
    assert (log[0].author_name, log[0].author_email) == ("Ann", "ann@example.com")
    assert log[0].changes == (("A", "a/b.yaml"),)
    committer = repo._git(["log", "-1", "--format=%cn <%ce>"]).decode().strip()
    assert committer == "Makna <makna@localhost>"
    assert repo.show(sha, "a/b.yaml") == b"x: 1\n"


def test_a_commit_is_built_from_the_bytes_given_and_touches_neither_tree_nor_index(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    (repo.path / "other.txt").write_text("staged by someone else\n")
    (repo.path / "a.yaml").write_text("edited in the working tree\n")
    raw(repo, "add", "other.txt")
    sha = commit_files(repo, {"a.yaml": None})
    assert repo.diff_names(sha) == [("D", "a.yaml")]
    assert repo.show(sha, "other.txt") is None  # what someone else staged is not swept in
    assert (
        repo.path / "a.yaml"
    ).read_text() == "edited in the working tree\n"  # not read, not written
    assert "other.txt" in raw(
        repo, "diff", "--cached", "--name-only"
    )  # their index is as they left it


def test_attributes_and_filters_of_the_working_tree_cannot_change_a_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    marker = tmp_path / "filter-ran"
    (repo.path / ".gitattributes").write_text(
        "*.yaml ident filter=evil text diff=evil working-tree-encoding=UTF-16\n"
    )
    raw(repo, "config", "filter.evil.clean", f"touch {marker}; cat")
    raw(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    raw(repo, "config", "diff.evil.textconv", f"sh -c 'touch {marker}; cat' --")
    text = "id: $Id: keep this $\ncafe: caf\u00e9\r\n"
    (repo.path / "a.yaml").write_text("a file in the working tree for a filter to find\n")
    made = repo.commit_files("m", "Ann", "ann@example.com", {"a.yaml": text.encode()}, None)
    assert repo.show(made.sha, "a.yaml") == text.encode()
    # Every other command the application runs, with a file in the working tree to run it on:
    repo.index_set(made.blobs)
    assert repo.log() and repo.regular_files(made.sha) == ["a.yaml"]
    assert repo.diff_names(made.sha) == [("A", "a.yaml")]
    assert repo.is_ancestor(made.sha) and repo.rev_parse("HEAD") == made.sha
    nxt = repo.commit_files("n", "Ann", "ann@example.com", {"b.yaml": b"b\n"}, made.sha)
    repo.index_set(nxt.blobs)
    assert not marker.exists()


def test_a_stale_head_is_refused_and_nothing_moves(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    first = commit_file(repo, "a.yaml", "1\n")
    commit_file(repo, "b.yaml", "2\n")
    head = repo.rev_parse("HEAD")
    with pytest.raises(RepoError) as caught:
        repo.commit_files("late", "Ann", "ann@example.com", {"c.yaml": b"3\n"}, first)
    assert caught.value.subcommand == "update-ref"
    assert repo.rev_parse("HEAD") == head


def test_show_reads_the_commit_not_the_working_tree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    sha = commit_file(repo, "a.yaml", "committed\n")
    (repo.path / "a.yaml").write_text("edited and not committed\n")
    assert repo.show(sha, "a.yaml") == b"committed\n"
    assert repo.show(sha, "missing.yaml") is None
    assert repo.read_blobs([(sha, "a.yaml"), (sha, "x\ny")]) == {
        (sha, "a.yaml"): b"committed\n",
        (sha, "x\ny"): None,
    }


def test_read_blobs_skips_what_is_not_a_blob_and_does_not_read_what_is_too_large(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "r")
    sha = commit_files(repo, {"d/a.yaml": b"x" * 100, "d/b.yaml": b"short\n"})
    got = repo.read_blobs(
        [(sha, "d"), (sha, "d/a.yaml"), (sha, "d/b.yaml"), (sha, "nothing")], max_size=50
    )
    assert got[(sha, "d")] is None  # a tree: its content is never read as a file
    assert got[(sha, "d/a.yaml")] == TooLarge(100)
    assert got[(sha, "d/b.yaml")] == b"short\n"
    assert got[(sha, "nothing")] is None
    assert repo.show(sha, "d/b.yaml") == b"short\n"


def test_an_author_name_is_cleaned_and_a_bad_email_is_refused(tmp_path: Path) -> None:
    assert (
        clean_display_name("Ann <ann@x>\nSigned-off-by: Eve\x00\x07")
        == "Ann ann@x Signed-off-by: Eve"
    )
    assert clean_display_name("<>\n") == "Makna user"
    assert clean_email("evil\x1b[31mRED\u202e@example.com\n") == "evil[31mRED@example.com"
    repo = make_repo(tmp_path / "r")
    with pytest.raises(ValueError):
        repo.commit_files("m", "Ann", "ann@x>\nGIT: yes", {"a.yaml": b"1"}, None)


def test_outside_author_names_and_emails_are_cleaned_when_read(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    (repo.path / "b.yaml").write_text("2\n")
    raw(repo, "add", "b.yaml")
    raw(repo, "commit", "-q", "-m", "x", GIT_AUTHOR_EMAIL="evil\x1b[31mRED\u202e@example.com",
        GIT_AUTHOR_NAME="Eve\x1b[0m")  # fmt: skip
    last = repo.log()[-1]
    assert "\x1b" not in last.author_email and "\u202e" not in last.author_email
    assert "\x1b" not in last.author_name


def test_a_hook_in_the_repository_does_not_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    marker = tmp_path / "hook-ran"
    for name in (
        "pre-commit",
        "commit-msg",
        "post-commit",
        "prepare-commit-msg",
        "reference-transaction",
    ):
        hook = repo.path / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    commit_file(repo, "a.yaml", "1\n")
    assert not marker.exists()


def test_the_config_of_the_machine_does_not_reach_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {home}/hooks\n[commit]\n\tgpgsign = true\n"
        f"[user]\n\tname = Wrong\n\temail = wrong@example.com\n"
    )
    hooks = home / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Wrong")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    assert not marker.exists()
    assert repo.log()[0].author_name == "Ann"


def test_a_failed_command_carries_the_subcommand_and_code_and_not_git_output(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "r")
    secret = "SECRET-FILE-CONTENT-token-123"
    commit_file(repo, "a.yaml", "1\n")
    with pytest.raises(RepoError) as caught:
        repo.commit_files("m", "Ann", "ann@example.com", {"b.yaml": secret.encode()}, None)
    error = caught.value
    assert error.subcommand == "update-ref" and error.returncode and error.returncode > 0
    assert secret not in str(error) and secret not in repr(error)
    assert "b.yaml" not in str(error)


def test_a_pathspec_cannot_be_an_option_or_leave_the_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    for bad in ("--all", "-A", "/etc/passwd", "../x", "a/../../x", "", "a\nb"):
        with pytest.raises(ValueError):
            repo.commit_files("m", "Ann", "ann@example.com", {bad: b"x"}, None)
    assert repo.rev_parse("HEAD") is None


def test_a_directory_inside_another_repository_is_not_a_repository_root(tmp_path: Path) -> None:
    outer = make_repo(tmp_path / "outer")
    inner = outer.path / "semantic" / "definitions"
    inner.mkdir(parents=True)
    assert is_repository_root(outer.path)
    assert not is_repository_root(inner)
    assert not is_repository_root(tmp_path / "missing")
    assert not is_repository_root(tmp_path)  # no repository there


def test_the_mode_of_a_path_never_falls_back_from_a_broken_repository(tmp_path: Path) -> None:
    outer = make_repo(tmp_path / "outer")
    inner = outer.path / "semantic" / "definitions"
    inner.mkdir(parents=True)
    assert repository_mode(outer.path, configured=True) is RepoMode.REPOSITORY
    assert repository_mode(inner, configured=True) is RepoMode.PLAIN  # inside another repository
    assert repository_mode(inner, configured=False) is RepoMode.PLAIN
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repository_mode(plain, configured=False) is RepoMode.PLAIN  # a directory given as is
    with pytest.raises(RepoUnusable):  # configured, not a repository, not inside one
        repository_mode(plain, configured=True)
    with pytest.raises(RepoUnusable):
        repository_mode(tmp_path / "missing", configured=True)

    def broken(name: str) -> Path:
        repo = make_repo(tmp_path / name)
        commit_file(repo, "a.yaml", "1\n")
        return repo.path

    bare = broken("bare")
    raw(GitRepo(bare), "config", "core.bare", "true")
    worktree = broken("worktree")
    raw(GitRepo(worktree), "config", "core.worktree", str(tmp_path))
    nogit = broken("nogit")
    import shutil

    shutil.rmtree(nogit / ".git")
    linked = broken("linked")
    shutil.move(str(linked / ".git"), str(tmp_path / "moved-git"))
    (linked / ".git").symlink_to(tmp_path / "moved-git")
    gitfile = broken("gitfile")
    shutil.rmtree(gitfile / ".git")
    (gitfile / ".git").write_text(f"gitdir: {bare / '.git'}\n")
    for path in (bare, worktree, nogit, linked, gitfile):
        for configured in (True, False):
            if path is nogit and not configured:
                assert repository_mode(path, configured=False) is RepoMode.PLAIN
                continue
            with pytest.raises(RepoUnusable) as caught:
                repository_mode(path, configured=configured)
            assert str(path) not in caught.value.message  # fixed text, no path


def test_is_ancestor_says_no_for_a_commit_that_is_gone(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    first = commit_file(repo, "a.yaml", "1\n")
    second = commit_file(repo, "b.yaml", "2\n")
    assert (
        repo.is_ancestor(first) and repo.is_ancestor(second) and repo.is_ancestor("0" * 40) is False
    )
    raw(repo, "reset", "-q", "--hard", first)
    assert repo.is_ancestor(first) and not repo.is_ancestor(second)


def test_a_bad_gitmodules_file_does_not_stop_the_history_from_being_read(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    raw_commit(repo, {"a.yaml": "1\n"})
    raw_commit(
        repo, {".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = ../x\n\tupdate = !echo hi\n'}
    )
    assert [c.changes for c in repo.log()][-1] == (("A", ".gitmodules"),)


def test_the_log_follows_a_merge_and_reads_odd_names(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    odd = "d/we ird\u00e9 name.yaml"
    raw_commit(repo, {"a.yaml": "1\n"})
    raw_commit(repo, {odd: "2\n"})
    raw(repo, "checkout", "-q", "-b", "side")
    raw_commit(repo, {"side.yaml": "s\n"})
    raw(repo, "checkout", "-q", "main")
    raw_commit(repo, {"main.yaml": "m\n"})
    raw(repo, "merge", "-q", "--no-ff", "side", "-m", "merge")
    log = repo.log()
    assert [c.changes for c in log][1] == (("A", odd),)
    assert log[-1].changes == (("A", "side.yaml"),)  # the merge brings in the side branch
    assert len(log[-1].parents) == 2


def test_symbolic_links_are_not_regular_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    raw_commit(repo, {"a.yaml": "1\n"})
    (repo.path / "link.yaml").symlink_to("/etc/passwd")
    raw(repo, "add", "link.yaml")
    raw(repo, "commit", "-q", "-m", "link")
    sha = repo.rev_parse("HEAD")
    assert sha is not None
    assert repo.regular_files(sha) == ["a.yaml"]


def test_index_set_makes_git_status_quiet_after_a_plumbing_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    made = repo.commit_files("m", "Ann", "ann@example.com", {"a.yaml": b"1\n"}, None)
    (repo.path / "a.yaml").write_bytes(b"1\n")
    repo.index_set(made.blobs)
    assert raw(repo, "status", "--porcelain") == ""


# --- layout and render ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("object_id", "domain", "proposal", "path"),
    [
        ("metric:npf_ratio", "lending", False, "lending/metrics/npf_ratio.yaml"),
        ("metric:npf_ratio", "lending", True, "lending/metrics/npf_ratio.draft.yaml"),
        ("entity:contract", "lending", False, "lending/entities/contract.yaml"),
        ("glossary_term:npf", "lending", True, "lending/glossary/npf.draft.yaml"),
        ("domain:lending", "lending", False, "lending/domain.yaml"),
        ("domain:lending", "lending", True, "lending/domain.draft.yaml"),
    ],
)
def test_a_path_is_built_from_an_id_and_parses_back(
    object_id: str, domain: str, proposal: bool, path: str
) -> None:
    ref = layout.parse_object_id(object_id)
    assert ref is not None
    assert layout.object_path(ref, domain, proposal=proposal) == path
    info = layout.parse_path(path)
    assert info is not None
    assert (info.ref, info.domain, info.is_proposal) == (ref, domain, proposal)


@pytest.mark.parametrize(
    "text", ["metric:../x", "metric:A", "metric:", "kind:x", "metric", "metric:x/y", "metric:x\n"]
)
def test_a_bad_id_gives_no_path(text: str) -> None:
    assert layout.parse_object_id(text) is None


@pytest.mark.parametrize("domain", ["..", "a/b", "Lending", "", "l\n", "a b"])
def test_a_bad_domain_gives_no_path(domain: str) -> None:
    with pytest.raises(ValueError):
        layout.object_path(layout.ObjectRef("metric", "x"), domain, proposal=False)


@pytest.mark.parametrize(
    "path",
    [
        "README.yaml", "lending/metrics/x.yml", "lending/other/x.yaml", "lending/metrics/X.yaml",
        "lending/metrics/sub/x.yaml", "../lending/metrics/x.yaml", "lending/metrics/.yaml",
        "lending/domain.yaml/x", "lending/entities/domain.yaml.draft.yaml",
    ],
)  # fmt: skip
def test_a_path_that_is_not_a_definition_file_is_not_parsed(path: str) -> None:
    assert layout.parse_path(path) is None


def test_every_sample_file_has_the_path_its_content_implies() -> None:
    for file in load_definitions(SAMPLE).files:
        info = layout.parse_path(file.path)
        assert info is not None and file.definition is not None
        assert (info.ref.kind, info.ref.name) == (file.definition.kind, file.definition.name)


def test_the_canonical_yaml_round_trips_every_sample_file() -> None:
    for file in load_definitions(SAMPLE).files:
        assert file.definition is not None
        text = render.to_yaml(file.definition)
        again, problems = render.parse_text(text)
        assert not problems and again is not None
        assert dump_definition(again) == dump_definition(file.definition)
        assert render.to_yaml(again) == text


def test_with_state_sets_the_fields_the_application_owns() -> None:
    metric = next(
        f.definition for f in load_definitions(SAMPLE).files if f.path.endswith("npf_ratio.yaml")
    )
    assert metric is not None
    drafted = render.with_state(metric, status="draft", version=7)
    dumped = dump_definition(drafted)
    assert dumped["status"] == "draft" and dumped["version"] == 7 and "deprecation" not in dumped
    assert metric.status == "approved"  # the original is not changed
    gone = render.with_state(
        metric, status="deprecated", version=2, deprecation=Deprecation(reason="old")
    )
    assert dump_definition(gone)["deprecation"] == {"reason": "old"}


def test_the_text_is_checked_as_a_model_and_the_size_limit_is_stated() -> None:
    assert render.MAX_YAML_BYTES == 256 * 1024
    definition, problems = render.parse_text("kind: metric\nname: x\n")
    assert definition is None and problems and all(p.code == "V01" for p in problems)
    definition, problems = render.parse_text("a: &x 1\nb: *x\n")
    assert definition is None and problems


def test_a_replace_ref_is_ignored_by_every_read(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    first = commit_file(repo, "a.yaml", "real\n")
    raw(repo, "checkout", "-q", "-b", "forge")
    (repo.path / "a.yaml").write_text("forged\n")
    raw(repo, "commit", "-q", "-am", "forged")
    forged = raw(repo, "rev-parse", "HEAD").strip()
    raw(repo, "checkout", "-q", "main")
    raw(repo, "replace", first, forged)
    assert raw(repo, "show", f"{first}:a.yaml") == "forged\n"  # git as a person runs it
    assert repo.show(first, "a.yaml") == b"real\n"  # as Makna runs it


@pytest.mark.parametrize("how", ["shallow", "grafts"])
def test_a_shallow_or_grafted_repository_is_not_a_usable_repository(
    tmp_path: Path, how: str
) -> None:
    repo = make_repo(tmp_path / "r")
    first = commit_file(repo, "a.yaml", "1\n")
    commit_file(repo, "b.yaml", "2\n")
    assert repository_mode(repo.path, configured=True) is RepoMode.REPOSITORY
    if how == "shallow":
        (repo.path / ".git" / "shallow").write_text(first + "\n")
    else:
        (repo.path / ".git" / "info").mkdir(exist_ok=True)
        (repo.path / ".git" / "info" / "grafts").write_text(f"{first}\n")
    with pytest.raises(RepoUnusable) as caught:
        repository_mode(repo.path, configured=True)
    assert caught.value.reason == "altered_history"
