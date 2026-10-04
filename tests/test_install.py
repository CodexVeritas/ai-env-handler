from pathlib import Path

import pytest

from envh.install import command

PASSWORDLESS_PROBLEM = "alice can run these with sudo without a password, so agents can too:\n  (root) NOPASSWD: /usr/local/bin/tool"


@pytest.fixture
def failing_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(command, "preflight_problems", lambda user: [PASSWORDLESS_PROBLEM])
    monkeypatch.setattr(command, "preflight_warnings", lambda: [])
    monkeypatch.setattr(command, "DATA_DIR", tmp_path / "data")


def test_install_refuses_when_the_system_checks_find_problems(failing_checks: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main("install", ["--dry-run"]) == 1
    output = capsys.readouterr()
    assert "  ! alice can run these with sudo without a password" in output.out
    assert "\n      (root) NOPASSWD: /usr/local/bin/tool" in output.out
    assert "--ignore-preflight" in output.err
    assert "would set up" not in output.out


def test_ignore_preflight_goes_ahead_once_the_problems_were_checked(failing_checks: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main("install", ["--dry-run", "--ignore-preflight"]) == 0
    assert "would set up" in capsys.readouterr().out
