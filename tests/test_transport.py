import pytest

from envh.client import transport


class FakeReminders:
    events: list[object] = []

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id

    def start(self) -> "FakeReminders":
        self.events.append(self.request_id)
        return self

    def stop(self) -> None:
        self.events.append("stopped")


def test_waiting_reminds_only_with_notify_on_and_a_desktop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    FakeReminders.events = []
    monkeypatch.setattr(transport, "Reminders", FakeReminders)
    monkeypatch.setenv("DISPLAY", ":0")
    with transport.waiting_notice({"request_id": 3, "notify": True}, None):
        assert FakeReminders.events == [3]
    assert FakeReminders.events == [3, "stopped"]
    with transport.waiting_notice({"request_id": 4, "notify": False}, None):
        pass
    with transport.waiting_notice({"request_id": 5}, None):
        pass
    monkeypatch.delenv("DISPLAY")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with transport.waiting_notice({"request_id": 6, "notify": True}, "envh session wait 6"):
        pass
    assert FakeReminders.events == [3, "stopped"]
    err = capsys.readouterr().err
    assert "waiting for approval in the envh console (request #3)" in err and "resume with: envh session wait 6" in err


def test_reminders_stop_even_when_the_wait_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeReminders.events = []
    monkeypatch.setattr(transport, "Reminders", FakeReminders)
    monkeypatch.setenv("DISPLAY", ":0")
    with pytest.raises(transport.ClientError):
        with transport.waiting_notice({"request_id": 7, "notify": True}, None):
            raise transport.ClientError("the broker closed the connection")
    assert FakeReminders.events == [7, "stopped"]
