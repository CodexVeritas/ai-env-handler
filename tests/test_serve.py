import getpass
import signal
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from envh.server import serve
from envh.server.init_cmd import PHRASE_FILE


def prompt_hit_by_ctrl_c(prompt: str) -> str:
    signal.raise_signal(signal.SIGINT)
    time.sleep(1)
    raise AssertionError("one Ctrl-C didn't interrupt the passphrase prompt")


@pytest.fixture
def restored_sigint() -> Iterator[None]:
    handler = signal.getsignal(signal.SIGINT)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    signal.signal(signal.SIGINT, handler)


@pytest.mark.usefixtures("restored_sigint")
def test_one_ctrl_c_at_the_passphrase_prompt_stops_the_console_and_ignores_a_passed_on_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / PHRASE_FILE).write_text("amber basil cedar\n")
    for check in ("assert_no_tiocsti", "assert_terminal_is_ours", "harden_process"):
        monkeypatch.setattr(serve, check, lambda: None)
    monkeypatch.setattr(serve, "shared_terminal_warning", lambda: None)
    monkeypatch.setattr(getpass, "getpass", prompt_hit_by_ctrl_c)
    assert serve.main(["--data-dir", str(tmp_path), "--socket", str(tmp_path / "envh.sock")]) == 130
    assert "Stopped." in capsys.readouterr().out
    signal.raise_signal(signal.SIGINT)
