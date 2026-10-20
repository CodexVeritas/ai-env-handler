import getpass
import signal
import time
from pathlib import Path

import pytest

from envh.server import serve
from envh.server.init_cmd import PHRASE_FILE


def prompt_hit_by_ctrl_c(prompt: str) -> str:
    signal.raise_signal(signal.SIGINT)
    time.sleep(1)
    raise AssertionError("one Ctrl-C didn't interrupt the passphrase prompt")


def test_one_ctrl_c_at_the_passphrase_prompt_stops_the_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / PHRASE_FILE).write_text("amber basil cedar\n")
    for check in ("assert_no_tiocsti", "assert_terminal_is_ours", "harden_process"):
        monkeypatch.setattr(serve, check, lambda: None)
    monkeypatch.setattr(serve, "shared_terminal_warning", lambda: None)
    monkeypatch.setattr(getpass, "getpass", prompt_hit_by_ctrl_c)
    previous_handler = signal.getsignal(signal.SIGINT)
    try:
        exit_code = serve.main(["--data-dir", str(tmp_path), "--socket", str(tmp_path / "envh.sock")])
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    assert exit_code == 130
    assert "Stopped." in capsys.readouterr().out
