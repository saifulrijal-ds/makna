"""Shared pieces of the WP-12 integration tests: a database with the sample catalog and
definitions indexed the way the service layer needs (with the `derive` hook), four users and a
token, and the callers made from them."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.engine import URL

from makna.catalog.file import FileConnector
from makna.catalog.sync import sync_catalog
from makna.definitions.loader import definition_objects, load_definitions
from makna.index import indexer, migrate
from makna.index.tables import api_token, app_user, tool_call_log
from makna.model.governance import STRICT, GovernancePolicy
from makna.search.embedding import HashProvider
from makna.services import Caller, Services
from makna.services.derive import make_derive
from tests.catalog_support import DEFINITIONS, EXPORT, TAG_MAP, sample_objects

ROLES = ("viewer", "curator", "approver", "admin")


@dataclass(frozen=True)
class Users:
    ids: Mapping[str, int]
    token_id: int

    def caller(self, role: str, channel: str = "mcp") -> Caller:
        teams = ("credit-risk",) if role == "curator" else ()
        return Caller(user_id=self.ids[role], role=role, channel=channel, teams=teams)

    def token(self, role: str = "admin", channel: str = "mcp") -> Caller:
        """A caller that came with the token of the user of `role` (the token's user is the
        admin in these tests, so the default shows that a token never gets more than a viewer)."""
        return Caller(
            user_id=self.ids[role], role=role, channel=channel, token_id=self.token_id
        )


def build(
    url: URL,
    *,
    policy: GovernancePolicy = STRICT,
    tag_map: Mapping[str, str] = TAG_MAP,
    export: Any = EXPORT,
    check_references: bool = True,
) -> Users:
    """Migrate, sync the sample catalog, index the sample definitions with the service layer's
    hook under `policy`, and add the users and a token."""
    migrate.upgrade(url.render_as_string(hide_password=False))
    engine = create_engine(url)
    try:
        entities, terms = sample_objects()
        sync_catalog(
            engine,
            FileConnector(export),
            tag_map=tag_map,
            entities=entities if check_references else None,
            glossary_terms=terms if check_references else None,
        )
        index(engine, policy)
        return add_users(engine)
    finally:
        engine.dispose()


def index(engine: Engine, policy: GovernancePolicy = STRICT) -> indexer.IndexResult:
    objects = definition_objects(load_definitions(DEFINITIONS))
    return indexer.build(engine, HashProvider(), objects, derive=make_derive(policy))


def add_users(engine: Engine) -> Users:
    ids: dict[str, int] = {}
    with engine.begin() as connection:
        for role in ROLES:
            ids[role] = connection.execute(
                insert(app_user)
                .values(
                    email=f"{role}@example.com",
                    display_name=role.title(),
                    password_hash="not-a-real-hash",
                    role=role,
                    teams=["credit-risk"] if role == "curator" else [],
                )
                .returning(app_user.c.id)
            ).scalar_one()
        token_id = connection.execute(
            insert(api_token)
            .values(user_id=ids["admin"], name="test", token_hash="0" * 64)
            .returning(api_token.c.id)
        ).scalar_one()
    return Users(ids, token_id)


def services(engine: Engine, policy: GovernancePolicy = STRICT) -> Services:
    return Services(engine, HashProvider(), policy=policy)


def last_log(engine: Engine) -> Mapping[str, Any]:
    """The newest `tool_call_log` row."""
    with engine.connect() as connection:
        row = connection.execute(
            select(tool_call_log).order_by(tool_call_log.c.id.desc()).limit(1)
        ).one()
    return row._asdict()


def log_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM tool_call_log")).scalar_one())
