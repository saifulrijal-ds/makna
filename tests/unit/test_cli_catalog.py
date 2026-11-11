"""WP-06: usage and error exits of `makna catalog` that need no database."""

from __future__ import annotations

from pathlib import Path

import pytest

from makna.cli.main import main
from makna.config import ConfigError, load_tag_map


def test_catalog_without_a_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog"]) == 2
    assert "choose sync or issues" in capsys.readouterr().err


def test_catalog_commands_need_the_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    assert main(["catalog", "issues"]) == 2
    assert main(["catalog", "sync"]) == 2
    assert capsys.readouterr().err.count("MAKNA_DATABASE_URL") == 2


def test_the_tag_map_is_read_without_the_secrets(tmp_path: Path) -> None:
    assert load_tag_map()["PII.Sensitive"] == "personal"
    assert load_tag_map(tmp_path / "missing.toml")["PII.NonSensitive"] == "confidential"


def test_a_bad_tag_map_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "m.toml"
    path.write_text('[sensitivity]\ntag_map = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_tag_map(path)
