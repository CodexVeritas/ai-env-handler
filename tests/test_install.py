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


def test_ignore_preflight_goes_ahead_and_says_what_it_ignored(failing_checks: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main("install", ["--dry-run", "--ignore-preflight"]) == 0
    output = capsys.readouterr().out
    assert "Going on despite 1 system check problem, as --ignore-preflight asks" in output
    assert "would set up" in output


def test_a_root_shell_without_sudo_is_refused_because_the_checks_need_the_account(failing_checks: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(command.os, "geteuid", lambda: 0)
    assert command.main("install", ["--dry-run"]) == 1
    assert "from your own account" in capsys.readouterr().err


def test_a_dry_run_without_root_on_an_installed_system_explains_instead_of_crashing(failing_checks: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    command.DATA_DIR.mkdir(mode=0o700)
    monkeypatch.setattr(command.os, "access", lambda path, mode: False)
    assert command.main("install", ["--dry-run", "--ignore-preflight"]) == 1
    assert "run the dry run with sudo" in capsys.readouterr().err
