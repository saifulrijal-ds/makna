"""WP-06: usage and error exits of `makna catalog` that need no database."""

from __future__ import annotations

from pathlib import Path

import pytest

from makna.cli.main import main
from makna.config import ConfigError, load_tag_map


def test_catalog_without_a_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalog"]) == 2
    assert "choose sync, issues or compare" in capsys.readouterr().err


def test_catalog_commands_need_the_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_DATABASE_URL", raising=False)
    assert main(["catalog", "issues"]) == 2
    assert main(["catalog", "sync"]) == 2
    assert capsys.readouterr().err.count("MAKNA_DATABASE_URL") == 2


def test_compare_needs_the_engine_url_and_the_database_url(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAKNA_ENGINE_URL", raising=False)
    monkeypatch.setenv("MAKNA_DATABASE_URL", "postgresql://u:p@localhost/none")
    assert main(["catalog", "compare"]) == 2
    assert "MAKNA_ENGINE_URL" in capsys.readouterr().err
    monkeypatch.setenv("MAKNA_ENGINE_URL", "postgresql://ro:pw@localhost/lending")
    monkeypatch.delenv("MAKNA_DATABASE_URL")
    assert main(["catalog", "compare"]) == 2
    assert "MAKNA_DATABASE_URL" in capsys.readouterr().err


def test_the_tag_map_is_read_without_the_secrets(tmp_path: Path) -> None:
    assert load_tag_map()["PII.Sensitive"] == "personal"
    assert load_tag_map(tmp_path / "missing.toml")["PII.NonSensitive"] == "confidential"


def test_a_bad_tag_map_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "m.toml"
    path.write_text('[sensitivity]\ntag_map = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_tag_map(path)


def test_official_objects_reports_invalid_files_with_their_object_ids(tmp_path: Path) -> None:
    from makna.definitions.loader import official_objects

    entities = tmp_path / "lending" / "entities"
    entities.mkdir(parents=True)
    (entities / "customer.yaml").write_text("kind: entity\nname: [broken\n", encoding="utf-8")
    (tmp_path / "lending" / "entities" / "other.draft.yaml").write_text(
        "kind: x\n", encoding="utf-8"
    )
    result = official_objects(tmp_path)
    assert result.entities == [] and result.terms == []
    assert result.invalid == {"entity:customer": "lending/entities/customer.yaml"}


def test_a_failing_stack_check_never_prints_an_empty_message(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from deploy import check_stack

    def check_bare() -> None:
        assert 1 + 1 == 3

    monkeypatch.setattr(check_stack, "CHECKS", [check_bare])
    assert check_stack.main() == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL check_bare: AssertionError: ")
    assert "1 + 1 == 3" in out
