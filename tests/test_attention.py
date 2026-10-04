import io
import subprocess
import time
import wave
from pathlib import Path

import pytest

from envh.client import attention
from envh.common import reminder_delays


def test_reminders_come_after_30s_1min_2min_5min_then_every_5min() -> None:
    delays = reminder_delays()
    assert [next(delays) for _ in range(7)] == [30, 30, 60, 180, 300, 300, 300]


def test_the_chime_is_a_short_mono_wav_that_never_clips() -> None:
    data = attention.chime_wav()
    assert data == attention.chime_wav()
    with wave.open(io.BytesIO(data)) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 22050)
        assert 1.2 < audio.getnframes() / audio.getframerate() < 1.4
        frames = audio.readframes(audio.getnframes())
    peak = max(abs(int.from_bytes(frames[index:index + 2], "little", signed=True)) for index in range(0, len(frames), 2))
    assert 0.55 * 32767 < peak <= 0.6 * 32767 + 1


def test_the_chime_file_is_written_once_into_the_runtime_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = attention.chime_file()
    assert path == tmp_path / attention.CHIME_FILE_NAME and path.read_bytes() == attention.chime_wav()
    path.write_bytes(b"kept")
    assert attention.chime_file().read_bytes() == b"kept"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert attention.chime_file() is None


@pytest.fixture
def gdbus_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake_run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        recorded.append(command)
        return subprocess.CompletedProcess(command, 0, f"(uint32 {40 + len(recorded)},)", "")

    monkeypatch.setattr(attention.subprocess, "run", fake_run)
    monkeypatch.setattr(attention.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return recorded


def test_a_notification_plays_the_chime_and_updates_in_place(gdbus_calls: list[list[str]], tmp_path: Path) -> None:
    assert attention.notify(3, 0, 0) == 41
    [first] = gdbus_calls
    assert first[:3] == ["/usr/bin/gdbus", "call", "--session"]
    assert first.index("--") < first.index("-1"), "a negative timeout must come after --, or gdbus reads it as an option"
    assert "Approve or deny request #3 in the envh console." in first
    assert f"'sound-file': <'{tmp_path / attention.CHIME_FILE_NAME}'>" in first[-2]
    assert attention.notify(3, 125, 41) == 42
    second = gdbus_calls[1]
    assert second[second.index("--") + 2] == "41"
    assert "Request #3 has been waiting 2 minutes. Approve or deny it in the envh console." in second


def test_reminders_repeat_until_stopped_then_remove_the_notification(gdbus_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attention, "reminder_delays", lambda: iter([0.05] * 100))
    reminders = attention.Reminders(3).start()
    time.sleep(0.3)
    reminders.stop()
    notifies = [call for call in gdbus_calls if "org.freedesktop.Notifications.Notify" in call]
    assert len(notifies) >= 3
    replaced = [call[call.index("--") + 2] for call in notifies]
    assert replaced == ["0", *(str(41 + index) for index in range(len(replaced) - 1))]
    assert "org.freedesktop.Notifications.CloseNotification" in gdbus_calls[-1] and gdbus_calls[-1][-1] == str(reminders.notification_id)
    count = len(gdbus_calls)
    time.sleep(0.15)
    assert len(gdbus_calls) == count


def test_a_missing_gdbus_is_reported_once(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(attention.shutil, "which", lambda name: None)
    monkeypatch.setattr(attention, "reminder_delays", lambda: iter([0.01] * 100))
    reminders = attention.Reminders(3).start()
    time.sleep(0.1)
    reminders.stop()
    assert capsys.readouterr().err.count("gdbus is missing") == 1
