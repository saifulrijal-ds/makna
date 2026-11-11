from __future__ import annotations

import pytest

from makna.cli.main import main


def test_help_exits_zero_and_describes_the_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--help"])
    assert info.value.code == 0
    assert "makna" in capsys.readouterr().out


def test_no_arguments_does_not_need_settings() -> None:
    assert main([]) == 0
