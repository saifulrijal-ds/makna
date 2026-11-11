"""WP-05: the minimal application (`/healthz`) and the `serve` command."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from makna.app import create_app
from makna.cli.main import main
from makna.config import ConfigError, load_settings

ENV = {"MAKNA_DATABASE_URL": "postgresql://u:p@localhost/makna", "MAKNA_SECRET_KEY": "k"}


def test_healthz_returns_ok() -> None:
    client = TestClient(create_app(load_settings(ENV, config_path="no-such-file.toml")))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_without_settings_names_the_missing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAKNA_SECRET_KEY", raising=False)
    with pytest.raises(ConfigError, match="MAKNA_DATABASE_URL"):
        create_app()


def test_serve_without_settings_exits_2_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    monkeypatch.delenv("MAKNA_SECRET_KEY", raising=False)
    assert main(["serve"]) == 2
    assert "MAKNA_DATABASE_URL" in capsys.readouterr().err


def test_serve_starts_the_application_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    assert main(["serve", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert calls == [
        (("makna.app:create_app",), {"factory": True, "host": "0.0.0.0", "port": 9000})
    ]
