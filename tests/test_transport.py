import subprocess

import pytest

from envh.client import transport


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, "(uint32 7,)", "")

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    monkeypatch.setattr(transport.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("DISPLAY", ":0")
    return recorded


def test_a_waiting_request_shows_a_fixed_notification_with_a_sound(calls: list[list[str]], capsys: pytest.CaptureFixture[str]) -> None:
    transport.waiting_notice({"request_id": 3, "notify": True}, None)
    [command] = calls
    assert command[:3] == ["/usr/bin/gdbus", "call", "--session"]
    assert command.index("--") < command.index("-1"), "a negative timeout must come after --, or gdbus reads it as an option"
    assert "envh needs your passphrase" in command and "Approve or deny request #3 in the envh console." in command
    assert "'sound-name': <'window-attention'>" in command[-2]
    assert "waiting for approval" in capsys.readouterr().err


def test_no_notification_when_notify_is_off_or_there_is_no_desktop(calls: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    transport.waiting_notice({"request_id": 3, "notify": False}, None)
    transport.waiting_notice({"request_id": 3}, None)
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    transport.waiting_notice({"request_id": 3, "notify": True}, None)
    assert calls == []


def test_a_missing_notifier_is_reported_in_one_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(transport.shutil, "which", lambda name: None)
    transport.notify_desktop(3)
    assert "gdbus is missing" in capsys.readouterr().err
