"""WP-16 acceptance 7, the account page, and `update_user` (integration tier)."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, select

from makna.index.tables import api_token, app_user
from makna.services import accounts
from makna.services.accounts import AccountError

from . import web_support as w

ROLES = ("viewer", "curator", "approver", "admin")


@pytest.fixture
def app(web_engine: Engine) -> Iterator[Any]:
    app = w.make_app(web_engine.url)
    yield app
    app.state.services.dispose()


def post(client: Any, path: str, data: dict[str, str], page: str) -> Any:
    return client.post(path, data={**data, "csrf_token": w.page_csrf(client, page)})


def alert(html: str) -> str:
    found = re.search(r'role="alert"><mark>([^<]*)<', html)
    assert found, html
    return found.group(1)


# --- criterion 7: what an admin can do ---


def test_an_admin_creates_a_user_who_can_then_sign_in(web_engine: Engine, app: Any) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    email = f"new-{secrets.token_hex(3)}@demo.example"
    password = secrets.token_urlsafe(18)
    response = post(
        admin,
        "/admin/users",
        {
            "email": email.upper(),
            "display_name": "New Person",
            "role": "curator",
            "teams": "Credit-Risk, data-governance,, ",
            "password": password,
        },
        "/admin/users",
    )
    assert (
        response.status_code == 303 and response.headers["location"] == "/admin/users?done=created"
    )
    created = accounts.get_user(web_engine, email)
    assert created is not None
    assert (created.role, created.teams, created.display_name) == (
        "curator",
        ("credit-risk", "data-governance"),
        "New Person",
    )
    page = admin.get(response.headers["location"])
    assert "The user was created." in page.text and email in page.text
    assert password not in page.text
    assert w.sign_in(w.make_client(app), email, password).status_code == 303


def test_creating_a_user_checks_the_input_and_never_shows_the_password(
    web_engine: Engine, app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    existing = w.make_user(web_engine)
    short = secrets.token_hex(4)
    caplog.set_level(logging.DEBUG)
    form = {
        "email": f"x-{secrets.token_hex(3)}@demo.example",
        "display_name": "X",
        "role": "viewer",
        "teams": "",
    }
    cases = {
        "a short password": {**form, "password": short},
        "no password": {**form, "password": ""},
        "an unknown role": {**form, "role": "root", "password": w.PASSWORD},
        "a bad email": {**form, "email": "no-at-sign", "password": w.PASSWORD},
        "a duplicate": {**form, "email": existing.email, "password": w.PASSWORD},
    }
    for label, data in cases.items():
        response = post(admin, "/admin/users", data, "/admin/users")
        assert response.status_code == 400, label
        assert alert(response.text)
        # The form is filled again, apart from the password.
        assert f'value="{data["email"]}"' in response.text
        for secret in (data["password"], w.PASSWORD):
            if secret:
                assert secret not in response.text, label
    assert accounts.get_user(web_engine, form["email"]) is None
    assert short not in caplog.text and w.PASSWORD not in caplog.text
    assert 'type="password"' in admin.get("/admin/users").text


def test_an_admin_changes_the_role_and_teams_of_a_user(web_engine: Engine, app: Any) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    target = w.make_user(web_engine, "viewer", teams=("a",))
    response = post(
        admin,
        f"/admin/users/{target.id}/update",
        {"role": "approver", "teams": "Data-Governance, credit-risk"},
        "/admin/users",
    )
    assert response.status_code == 303
    changed = accounts.get_user(web_engine, target.email)
    assert changed is not None
    assert (changed.role, changed.teams) == ("approver", ("data-governance", "credit-risk"))
    # Teams can be emptied.
    post(
        admin, f"/admin/users/{target.id}/update", {"role": "approver", "teams": ""}, "/admin/users"
    )
    again = accounts.get_user(web_engine, target.email)
    assert again is not None and again.teams == ()
    bad = post(
        admin, f"/admin/users/{target.id}/update", {"role": "root", "teams": ""}, "/admin/users"
    )
    assert bad.status_code == 400 and "role" in alert(bad.text)
    assert (
        post(admin, "/admin/users/999999/update", {"role": "viewer"}, "/admin/users").status_code
        == 404
    )


def test_an_admin_deactivates_a_user_who_can_then_no_longer_sign_in(
    web_engine: Engine, app: Any
) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    target = w.make_user(web_engine, "curator")
    token = accounts.create_token(web_engine, target.email, "agent")
    session = w.signed_in(app, target)
    assert session.get("/account").status_code == 200
    response = post(admin, f"/admin/users/{target.id}/deactivate", {}, "/admin/users")
    assert response.status_code == 303 and response.headers["location"].endswith("done=deactivated")
    assert w.sign_in(w.make_client(app), target.email).status_code == 401
    assert session.get("/account").status_code == 303  # the open session ends at once
    with pytest.raises(accounts.AuthenticationError):
        accounts.authenticate_token(web_engine, token.token, "mcp")  # and so does the token
    assert "inactive" in admin.get("/admin/users").text


def test_an_admin_revokes_any_token_on_the_tokens_page(web_engine: Engine, app: Any) -> None:
    admin = w.signed_in(app, w.make_user(web_engine, "admin"))
    owner = w.make_user(web_engine, "viewer")
    token = accounts.create_token(web_engine, owner.email, "somebody else's agent")
    page = admin.get("/admin/tokens")
    assert "somebody else&#39;s agent" in page.text and owner.email in page.text
    assert token.token not in page.text
    response = post(admin, f"/admin/tokens/{token.id}/revoke", {}, "/admin/tokens")
    assert (
        response.status_code == 303 and response.headers["location"] == "/admin/tokens?done=revoked"
    )
    with pytest.raises(accounts.AuthenticationError):
        accounts.authenticate_token(web_engine, token.token, "mcp")
    # Revoking twice is the same answer; an unknown id is a page that does not exist.
    assert post(admin, f"/admin/tokens/{token.id}/revoke", {}, "/admin/tokens").status_code == 303
    assert post(admin, "/admin/tokens/999999/revoke", {}, "/admin/tokens").status_code == 404


@pytest.mark.parametrize("role", ["viewer", "curator", "approver"])
def test_every_other_role_gets_403_on_every_admin_route(
    web_engine: Engine, app: Any, role: str
) -> None:
    client = w.signed_in(app, w.make_user(web_engine, role))
    token = w.page_csrf(client)
    target = w.make_user(web_engine, "viewer")
    admin_routes = [
        (path, methods) for path, methods in w.iter_routes(app) if path.startswith("/admin")
    ]
    assert len(admin_routes) >= 6
    for path, methods in admin_routes:
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            response = client.request(
                method,
                w.fill(path),
                data={"csrf_token": token, "role": "admin", "email": target.email},
            )
            assert response.status_code == 403, (role, method, path)
            assert "do not have access" in response.text
    # Nothing was changed.
    unchanged = accounts.get_user(web_engine, target.email)
    assert unchanged is not None and unchanged.role == "viewer"


def test_a_token_of_an_admin_has_no_web_access_by_itself(web_engine: Engine, app: Any) -> None:
    """A token is for `/mcp`; the pages take the session cookie only."""
    admin = w.make_user(web_engine, "admin")
    token = accounts.create_token(web_engine, admin.email, "t").token
    response = w.make_client(app).get("/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 303


# --- guards ---


def test_an_admin_cannot_deactivate_themself_or_remove_their_own_admin_role(
    web_engine: Engine, app: Any
) -> None:
    me = w.make_user(web_engine, "admin")
    admin = w.signed_in(app, me)
    response = post(admin, f"/admin/users/{me.id}/deactivate", {}, "/admin/users")
    assert response.status_code == 400 and "your own account" in alert(response.text)
    response = post(
        admin, f"/admin/users/{me.id}/update", {"role": "viewer", "teams": ""}, "/admin/users"
    )
    assert response.status_code == 400 and "own admin role" in alert(response.text)
    now = accounts.get_user(web_engine, me.email)
    assert now is not None and now.is_active and now.role == "admin"
    # Their own teams can be changed.
    assert (
        post(
            admin, f"/admin/users/{me.id}/update", {"role": "admin", "teams": "x"}, "/admin/users"
        ).status_code
        == 303
    )


def test_the_account_store_keeps_at_least_one_active_admin(migrated_engine: Engine) -> None:
    """The guard behind the pages: not a rule of the page alone, so two admins who demote each
    other cannot leave nobody. It needs a database in which these two are the only admins."""
    web_engine = migrated_engine
    only = w.make_user(web_engine, "admin")
    store = accounts.AccountStore(web_engine)
    with pytest.raises(AccountError, match="at least one active admin"):
        store.update_user(only.email, role="viewer", teams=None)
    with pytest.raises(AccountError, match="at least one active admin"):
        store.deactivate_user(only.email)
    # Changing the teams of the last admin is fine, and so is leaving the role as admin.
    assert store.update_user(only.email, role="admin", teams=["x"]).teams == ("x",)
    second = w.make_user(web_engine, "admin")
    assert store.update_user(only.email, role="viewer", teams=None).role == "viewer"
    with pytest.raises(AccountError, match="at least one active admin"):
        store.deactivate_user(second.email)  # now the last one
    # The operator's path (no `keep_an_admin`) is not limited.
    assert accounts.deactivate_user(web_engine, second.email).is_active is False


# --- update_user ---


def test_update_user_checks_like_create_user_and_changes_only_what_is_given(
    web_engine: Engine,
) -> None:
    user = w.make_user(web_engine, "viewer", teams=("a", "b"))
    assert accounts.update_user(web_engine, user.email.upper(), role="curator").teams == ("a", "b")
    changed = accounts.update_user(
        web_engine, user.email, teams=[" Credit-Risk ", "credit-risk", "x"]
    )
    assert (changed.role, changed.teams) == ("curator", ("credit-risk", "x"))
    assert accounts.update_user(web_engine, user.email, teams=[]).teams == ()
    both = accounts.update_user(web_engine, user.email, role="approver", teams=["t"])
    assert (both.role, both.teams) == ("approver", ("t",))
    for bad in (
        {"role": "root"},
        {"role": ""},
        {"teams": "a,b"},
        {"teams": ["", "a"]},
        {"teams": [1]},
        {},
    ):
        with pytest.raises(AccountError):
            accounts.update_user(web_engine, user.email, **bad)
    with pytest.raises(AccountError, match="no user"):
        accounts.update_user(web_engine, "nobody@demo.example", role="viewer")
    with pytest.raises(AccountError, match="email"):
        accounts.update_user(web_engine, "not an email", role="viewer")
    # Nothing a failed call asked for was applied.
    after = accounts.get_user(web_engine, user.email)
    assert after is not None and (after.role, after.teams) == ("approver", ("t",))
    # The password is untouched by a role change.
    assert accounts.verify_password(web_engine, user.email, w.PASSWORD) is not None


def test_get_user_by_id(web_engine: Engine) -> None:
    user = w.make_user(web_engine)
    assert accounts.get_user_by_id(web_engine, user.id) == user
    assert accounts.get_user_by_id(web_engine, 99_999_999) is None


def test_the_account_store_turns_a_database_error_into_the_fixed_one() -> None:
    from sqlalchemy import create_engine

    store = accounts.AccountStore(
        create_engine("postgresql://u:" + secrets.token_hex(4) + "@127.0.0.1:1/x")
    )
    for call in (
        lambda: store.list_users(),
        lambda: store.get_user(1),
        lambda: store.list_tokens(),
        lambda: store.revoke_token(1),
        lambda: store.create_token("a@demo.example", "n"),
        lambda: store.update_user("a@demo.example", role="viewer", teams=None),
        lambda: store.deactivate_user("a@demo.example"),
        lambda: store.verify_password("a@demo.example", w.PASSWORD),
        lambda: store.caller_for_user(1, "web"),
    ):
        with pytest.raises(accounts.AccountStoreError) as raised:
            call()
        assert str(raised.value) == "the user store is not available"


# --- the user's own account ---


def test_the_account_page_shows_the_own_tokens_only_and_a_new_token_once(
    web_engine: Engine, app: Any
) -> None:
    me = w.make_user(web_engine, "curator", name="Rina Wijaya")
    other = w.make_user(web_engine)
    mine_old = accounts.create_token(web_engine, me.email, "my old agent")
    theirs = accounts.create_token(web_engine, other.email, "their agent")
    client = w.signed_in(app, me)
    page = client.get("/account")
    assert "my old agent" in page.text and "their agent" not in page.text
    assert me.email in page.text and "Rina Wijaya" in page.text
    assert mine_old.token not in page.text and theirs.token not in page.text
    created = post(client, "/account/tokens", {"name": "my notebook"}, "/account")
    assert created.status_code == 200
    shown = re.search(r"<code>(mkn_[A-Za-z0-9_-]{43})</code>", created.text)
    assert shown, created.text
    assert "shown only once" in created.text
    # It works as a bearer token, and it is stored as a hash only.
    caller = accounts.authenticate_token(web_engine, shown.group(1), "mcp")
    assert caller.user_id == me.id
    with web_engine.connect() as connection:
        rows = connection.execute(select(api_token.c.token_hash)).scalars().all()
    assert shown.group(1) not in rows and accounts.hash_token(shown.group(1)) in rows
    # No later page shows it again.
    assert shown.group(1) not in client.get("/account").text
    assert "my notebook" in client.get("/account").text
    # An empty name is refused.
    refused = post(client, "/account/tokens", {"name": "  "}, "/account")
    assert refused.status_code == 400 and "token name" in alert(refused.text)


def test_revoking_a_token_that_is_not_yours_is_the_same_404_as_an_unknown_id(
    web_engine: Engine, app: Any
) -> None:
    me = w.make_user(web_engine)
    other = w.make_user(web_engine)
    mine = accounts.create_token(web_engine, me.email, "mine")
    theirs = accounts.create_token(web_engine, other.email, "theirs")
    client = w.signed_in(app, me)
    foreign = post(client, f"/account/tokens/{theirs.id}/revoke", {}, "/account")
    unknown = post(client, "/account/tokens/999999999/revoke", {}, "/account")
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.text == unknown.text
    # The other token still works; mine can be revoked.
    assert accounts.authenticate_token(web_engine, theirs.token, "mcp")
    done = post(client, f"/account/tokens/{mine.id}/revoke", {}, "/account")
    assert done.status_code == 303 and done.headers["location"] == "/account?done=revoked"
    with pytest.raises(accounts.AuthenticationError):
        accounts.authenticate_token(web_engine, mine.token, "mcp")
    assert "revoked" in client.get("/account").text


def test_an_admin_sees_other_users_tokens_only_on_the_tokens_page(
    web_engine: Engine, app: Any
) -> None:
    admin = w.make_user(web_engine, "admin")
    other = w.make_user(web_engine)
    accounts.create_token(web_engine, other.email, "visible to the admin page only")
    client = w.signed_in(app, admin)
    assert "visible to the admin page only" not in client.get("/account").text
    assert "visible to the admin page only" in client.get("/admin/tokens").text
    # The admin cannot revoke someone else's token from /account either.
    token_id = accounts.list_tokens(web_engine, other.email)[0].id
    assert post(client, f"/account/tokens/{token_id}/revoke", {}, "/account").status_code == 404


def test_two_admins_demoting_or_deactivating_each_other_at_once_leave_an_admin(
    migrated_engine: Engine,
) -> None:
    import threading

    engine = migrated_engine
    store = accounts.AccountStore(engine)
    for round_number in range(20):
        with engine.begin() as connection:  # only the two new admins are active admins
            connection.execute(
                app_user.update().where(app_user.c.role == "admin").values(is_active=False)
            )
        first = w.make_user(engine, "admin")
        second = w.make_user(engine, "admin")
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def change(
            target: str,
            deactivate: bool,
            barrier: threading.Barrier = barrier,
            outcomes: list[str] = outcomes,
        ) -> None:
            barrier.wait()
            try:
                if deactivate:
                    store.deactivate_user(target)
                else:
                    store.update_user(target, role="viewer", teams=None)
                outcomes.append("done")
            except AccountError:
                outcomes.append("refused")

        deactivate = round_number % 2 == 1
        threads = [
            threading.Thread(target=change, args=(first.email, deactivate)),
            threading.Thread(target=change, args=(second.email, deactivate)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with engine.connect() as connection:
            active = connection.execute(
                select(app_user.c.id).where(
                    app_user.c.role == "admin", app_user.c.is_active.is_(True)
                )
            ).all()
        assert len(active) >= 1, (round_number, outcomes)
        assert sorted(outcomes) == ["done", "refused"], (round_number, outcomes)
