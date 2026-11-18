Run git with an empty environment, no hooks, no machine config, and never keep its stderr.

Seen while building WP-18 (2026-11-18).

- Git reads `~/.gitconfig`, `GIT_*` variables, hooks, and parent repositories. Each can run a program or change which repository is used. `makna.definitions.repo.GitRepo` builds its environment from nothing, sets `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`, `--no-verify`, and names `--git-dir` and `--work-tree` so a parent repository is never found.
- A directory inside another repository (`semantic/definitions/` in the source tree) is a plain directory; only a path that is the top level of its own repository is writable.
- `git commit --only -- paths` commits just those paths, so something staged by someone else is not swept in. After a failed commit `discard` restores the working tree without git, because a stale `index.lock` makes every git command fail.
- Git's stderr can hold file content and paths: `RepoError` carries the subcommand and exit code only.
- Tests: `tests/unit/test_definition_repo.py`, `tests/integration/test_lifecycle.py`.
