"""WP-18: `makna.definitions.repo.GitRepo`, the layout of paths, and the canonical YAML.

These need the `git` command line (no database): a temporary repository per test.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from makna.definitions import layout, render
from makna.definitions.loader import load_definitions
from makna.definitions.repo import (
    GitRepo,
    RepoError,
    clean_display_name,
    is_repository_root,
)
from makna.model.definitions import Deprecation, dump_definition

SAMPLE = Path(__file__).resolve().parents[2] / "semantic" / "definitions"


def make_repo(path: Path) -> GitRepo:
    repo = GitRepo(path)
    repo.init()
    return repo


def commit_file(repo: GitRepo, name: str, text: str, *, who: str = "ann@example.com") -> str:
    target = repo.path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    repo.add([name])
    return repo.commit(f"edit({name}): test", "Ann", who, [name])


def test_a_commit_has_the_user_as_author_and_makna_as_committer(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    sha = commit_file(repo, "a/b.yaml", "x: 1\n")
    log = repo.log()
    assert [c.sha for c in log] == [sha]
    assert (log[0].author_name, log[0].author_email) == ("Ann", "ann@example.com")
    assert log[0].changes == (("A", "a/b.yaml"),)
    committer = repo._git(["log", "-1", "--format=%cn <%ce>"]).decode().strip()
    assert committer == "Makna <makna@localhost>"


def test_commit_takes_only_the_named_paths_and_a_deletion_is_a_change(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    (repo.path / "other.txt").write_text("staged by someone else\n")
    repo._git(["add", "other.txt"])
    repo.rm(["a.yaml"])
    sha = repo.commit("remove", "Ann", "ann@example.com", ["a.yaml"])
    assert repo.diff_names(sha) == [("D", "a.yaml")]
    assert repo.show(sha, "other.txt") is None  # still only staged


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


def test_an_author_name_is_cleaned_and_a_bad_email_is_refused(tmp_path: Path) -> None:
    assert (
        clean_display_name("Ann <ann@x>\nSigned-off-by: Eve\x00\x07")
        == "Ann ann@x Signed-off-by: Eve"
    )
    assert clean_display_name("<>\n") == "Makna user"
    repo = make_repo(tmp_path / "r")
    (repo.path / "a.yaml").write_text("1\n")
    repo.add(["a.yaml"])
    with pytest.raises(ValueError):
        repo.commit("m", "Ann", "ann@x>\nGIT: yes", ["a.yaml"])


def test_a_hook_in_the_repository_does_not_run(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    marker = tmp_path / "hook-ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "prepare-commit-msg"):
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
    (repo.path / "a.yaml").write_text(secret)
    with pytest.raises(RepoError) as caught:
        repo.add(["does-not-exist.yaml"])
    error = caught.value
    assert error.subcommand == "add" and error.returncode and error.returncode > 0
    assert secret not in str(error) and secret not in repr(error)
    assert "does-not-exist" not in str(error)


def test_a_pathspec_cannot_be_an_option_or_leave_the_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    for bad in ("--all", "-A", "/etc/passwd", "../x", "a/../../x", ""):
        with pytest.raises(ValueError):
            repo.add([bad])
    (repo.path / "-A").write_text("a file named like an option\n")
    repo.add(["./-A"])  # a path that merely looks like one is fine after `--`
    assert repo.commit("m", "Ann", "ann@example.com", ["./-A"])


def test_a_directory_inside_another_repository_is_not_a_repository_root(tmp_path: Path) -> None:
    outer = make_repo(tmp_path / "outer")
    inner = outer.path / "semantic" / "definitions"
    inner.mkdir(parents=True)
    assert is_repository_root(outer.path)
    assert not is_repository_root(inner)
    assert not is_repository_root(tmp_path / "missing")
    assert not is_repository_root(tmp_path)  # no repository there


def test_the_log_follows_a_merge_and_reads_odd_names(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    odd = "d/we irdé name.yaml"
    commit_file(repo, odd, "2\n", who="eve@example.com")
    repo._git(["checkout", "-q", "-b", "side"])
    commit_file(repo, "side.yaml", "s\n")
    repo._git(["checkout", "-q", "main"])
    commit_file(repo, "main.yaml", "m\n")
    repo._git(
        ["merge", "-q", "--no-ff", "side", "-m", "merge"],
        extra_env={
            "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x",
            "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x",
        },
    )  # fmt: skip
    log = repo.log()
    assert [c.changes for c in log][1] == (("A", odd),)
    assert log[-1].changes == (("A", "side.yaml"),)  # the merge brings in the side branch
    assert len(log[-1].parents) == 2


def test_symbolic_links_are_not_regular_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "1\n")
    (repo.path / "link.yaml").symlink_to("/etc/passwd")
    repo.add(["link.yaml"])
    sha = repo.commit("link", "Ann", "ann@example.com", ["link.yaml"])
    assert repo.regular_files(sha) == ["a.yaml"]


def test_discard_puts_the_committed_state_back(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    commit_file(repo, "a.yaml", "old\n")
    (repo.path / "a.yaml").write_text("new\n")
    (repo.path / "b.yaml").write_text("fresh\n")
    repo.add(["a.yaml", "b.yaml"])
    repo.discard(["a.yaml", "b.yaml"])
    assert (repo.path / "a.yaml").read_text() == "old\n"
    assert not (repo.path / "b.yaml").exists()
    assert repo._git(["status", "--porcelain"]) == b""


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
