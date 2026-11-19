"""Shared pieces of the WP-18 tests: a scratch database with the sample catalog, a temporary git
repository seeded from the sample definitions, services that write to it, and users made for the
two-person and owner-team rules."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, insert, select
from sqlalchemy.engine import URL

from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.definitions.render import to_yaml
from makna.definitions.repo import GitRepo
from makna.index import migrate
from makna.index.tables import api_token, app_user, audit_event
from makna.model.definitions import parse_definition
from makna.model.governance import STRICT
from makna.search.embedding import HashProvider
from makna.services import Caller, Services
from makna.services.approval import ApprovalSettings
from tests.catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects

# name -> (role, teams)
USERS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "viewer": ("viewer", ()),
    "curator": ("curator", ("credit-risk",)),
    "curator2": ("curator", ("credit-risk",)),
    "sales": ("curator", ("sales-performance",)),
    "outsider": ("curator", ("marketing",)),
    "outsider2": ("curator", ("marketing",)),
    "approver": ("approver", ()),
    "approver2": ("approver", ()),
    "governance": ("approver", ("data-governance",)),
    "admin": ("admin", ()),
}

NEW_METRIC = """\
apiVersion: makna/v1
kind: metric
name: contract_count_total
label: Total Contract Count
description: >-
  The number of contracts in the portfolio on the last snapshot, counted once each.
domain: lending
owner: {team: credit-risk, contact: credit-risk@example.com}
status: draft
sensitivity: internal
synonyms:
  en: [total contracts, number of contracts]
  id: [jumlah kontrak total, total kontrak]
type: simple
measure: contract_snapshot.active_contracts
join_paths:
  branch: [contract_snapshot.contract, contract.servicing_branch]
format: {kind: count, unit: contracts, decimals: 0}
time_grains: [month, quarter, year]
default_grain: month
"""


@dataclass
class World:
    engine: Engine
    url: URL
    repo: GitRepo
    services: Services
    ids: Mapping[str, int]
    emails: Mapping[str, str]
    token_id: int

    def caller(self, name: str, channel: str = "web") -> Caller:
        role, teams = USERS[name]
        return Caller(user_id=self.ids[name], role=role, channel=channel, teams=teams)

    def token(self, name: str = "admin") -> Caller:
        role, teams = USERS[name]
        return Caller(
            user_id=self.ids[name],
            role=role,
            channel="mcp",
            teams=teams,
            token_id=self.token_id,
        )

    @property
    def life(self) -> Any:
        return self.services.lifecycle


def git(repo: GitRepo, *args: str, env: Mapping[str, str] | None = None) -> str:
    """A raw git command in the repository, as someone outside Makna would run it."""
    import os

    full = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Outside Person",
        "GIT_AUTHOR_EMAIL": "outside@example.com",
        "GIT_COMMITTER_NAME": "Outside Person",
        "GIT_COMMITTER_EMAIL": "outside@example.com",
        **(env or {}),
    }
    done = subprocess.run(
        ["git", "-C", str(repo.path), *args],
        capture_output=True,
        text=True,
        check=True,
        env=full,
    )
    return done.stdout


def is_open(row: Mapping[str, Any]) -> bool:
    """An intent that no commit completed and nobody gave up."""
    return row["commit_sha"] is None and row["failed_at"] is None


def audit_rows(engine: Engine) -> list[Mapping[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(select(audit_event).order_by(audit_event.c.id)).all()
    return [row._asdict() for row in rows]


def build_world(
    url: URL,
    repo_path: Path,
    *,
    allow_self_approval: bool = False,
    seed: bool = True,
) -> World:
    migrate.upgrade(url.render_as_string(hide_password=False))
    engine = create_engine(url)
    entities, terms = sample_objects()
    sync_catalog(
        engine, FileConnector(EXPORT), tag_map=TAG_MAP, entities=entities, glossary_terms=terms
    )
    ids: dict[str, int] = {}
    emails: dict[str, str] = {}
    with engine.begin() as connection:
        for name, (role, teams) in USERS.items():
            emails[name] = f"{name}@example.com"
            ids[name] = connection.execute(
                insert(app_user)
                .values(
                    email=emails[name],
                    display_name=name.title(),
                    password_hash="not-a-real-hash",
                    role=role,
                    teams=list(teams),
                )
                .returning(app_user.c.id)
            ).scalar_one()
        token_id = connection.execute(
            insert(api_token)
            .values(user_id=ids["admin"], name="test", token_hash="0" * 64)
            .returning(api_token.c.id)
        ).scalar_one()
    services = Services(
        engine,
        HashProvider(),
        policy=STRICT,
        definitions_repo=str(repo_path),
        approval=ApprovalSettings("data-governance", allow_self_approval),
    )
    if seed:
        from makna.services.lifecycle import seed_repository

        seed_repository(engine, repo_path, DEFINITIONS)
        assert services.lifecycle.sync() is not None
    return World(engine, url, GitRepo(repo_path), services, ids, emails, token_id)


def metric_text(**changes: Any) -> str:
    """The new metric of the tests as YAML, with top-level keys replaced (None removes a key)."""
    import yaml

    data = yaml.safe_load(NEW_METRIC)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return yaml.safe_dump(data, sort_keys=False)


def official_text(world: World, object_id: str) -> str:
    """The official file of an object in the canonical YAML, as the index holds it."""
    from makna.index.tables import semantic_object

    with world.engine.connect() as connection:
        official = connection.execute(
            select(semantic_object.c.official).where(semantic_object.c.id == object_id)
        ).scalar_one()
    return to_yaml(parse_definition(official))


def edited(text: str, edit: Any) -> str:
    """`text` (YAML of one definition) after `edit(data)` changed the decoded mapping."""
    import yaml

    data = yaml.safe_load(text)
    edit(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def head(world: World) -> str:
    sha = world.repo.rev_parse("HEAD")
    assert sha is not None
    return sha


def object_row(world: World, object_id: str) -> Mapping[str, Any]:
    from makna.index.tables import semantic_object

    with world.engine.connect() as connection:
        row = connection.execute(
            select(semantic_object).where(semantic_object.c.id == object_id)
        ).one_or_none()
    assert row is not None, object_id
    return row._asdict()


def has_object(world: World, object_id: str) -> bool:
    from makna.index.tables import semantic_object

    with world.engine.connect() as connection:
        return (
            connection.execute(
                select(semantic_object.c.id).where(semantic_object.c.id == object_id)
            ).first()
            is not None
        )


def state_dump(engine: Engine) -> dict[str, list[Any]]:
    """Every row of the tables the write path and the indexer touch (for "changes nothing")."""
    from sqlalchemy import text

    dump: dict[str, list[Any]] = {}
    with engine.connect() as connection:
        for table, order in (
            ("audit_event", "id"),
            ("semantic_object", "id"),
            ("object_edge", "src_id, dst_id, type"),
            ("index_state", "key"),
            ("reference_issue", "id"),
        ):
            rows = connection.execute(text(f"SELECT * FROM {table} ORDER BY {order}")).all()  # noqa: S608
            dump[table] = [tuple(r) for r in rows]
        rows = connection.execute(
            text("SELECT object_id, status, text_hash FROM search_document ORDER BY object_id")
        ).all()
        dump["search_document"] = [tuple(r) for r in rows]
    return dump


def working_tree(world: World) -> str:
    """`git status --porcelain` of the working tree (empty when nothing differs from the index)."""
    done = subprocess.run(
        ["git", "-C", str(world.repo.path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def snapshot(world: World) -> tuple[Any, ...]:
    """Everything a read-only action must leave as it was: `HEAD`, the working tree, the files in
    it, and every row of the tables the write path touches (including `audit_event` and
    `semantic_object`)."""
    return (
        head(world),
        working_tree(world),
        state_dump(world.engine),
        sorted(
            str(p.relative_to(world.repo.path))
            for p in world.repo.path.rglob("*")
            if ".git" not in p.parts
        ),
    )
