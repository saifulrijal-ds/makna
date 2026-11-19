Run git with an empty environment, no hooks, no machine config, build commits with plumbing from the checked bytes, and never keep its stderr.

Seen while building WP-18 (2026-11-18), changed after the reviews.

- Git reads `~/.gitconfig`, `GIT_*` variables, hooks, and parent repositories. `makna.definitions.repo.GitRepo` builds its environment from nothing, sets `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, and names `--git-dir` (no work tree), so a parent repository and the working tree's `.gitmodules` are never read.
- `git add` and `git commit` apply `.gitattributes` (`ident`, `working-tree-encoding`, `eol`) and run `filter` commands, and a raw commit can add `.gitattributes`. Committing from the working tree therefore let an outsider change what the application committed (the commit no longer matched its intent). The application commits with plumbing: `hash-object -w --no-filters`, a private `GIT_INDEX_FILE`, `write-tree`, `commit-tree`, `update-ref HEAD new old`. The working tree is not authoritative.
- A directory inside another repository (`semantic/definitions/` in the source tree) is a plain directory; but a configured path whose `.git` is broken (bare, re-rooted, a link, another owner) must be an error, never a fallback to reading its files (`repository_mode`).
- A history rewrite (reset, amend, forced move) silently drops approvals if official content is read from `HEAD` alone: refuse when `last_indexed_commit` is not an ancestor of `HEAD`.
- Git's stderr can hold file content and paths: `RepoError` carries the subcommand and exit code only.
- Tests: `tests/unit/test_definition_repo.py`, `tests/integration/test_lifecycle.py`, `tests/integration/test_lifecycle_safety.py`.
