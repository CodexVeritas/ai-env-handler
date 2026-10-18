import fcntl
import os
import re
import select
import struct
import subprocess
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

import pytest

from envh.client.transport import ClientError
from envh.common import peek
from envh.tools.explorer import ANSI, REVEAL_SECONDS, Explorer, explore, load_scan, newest_scan, split_keys
from envh.tools.scanner import scan_paths, save_results

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "sk-or-v1-" + "0123456789abcdef" * 4
ANTHROPIC = "sk-ant-api03-" + "Zz9yY8xX7wW6vV5u" * 3
FRED = "3f2a9c8b7d6e5f4a3b2c1d0e9f8a7b6c"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def saved_scan(tmp_path: Path, files: dict[str, str]) -> Path:
    project = tmp_path / "project"
    for name, text in files.items():
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text)
    hits, scanned, unreadable = scan_paths([project], 1024 * 1024)
    return save_results(hits, scanned, unreadable, 1.0, [project], tmp_path / "scans", datetime(2026, 10, 17, 20, 0, 0))


def screen(explorer: Explorer, width: int = 120, height: int = 30) -> list[str]:
    return [ANSI.sub("", row) for row in explorer.render(width, height)]


def press(explorer: Explorer, *keys: str) -> None:
    for key in keys:
        explorer.handle(key, 30)


def test_the_tree_opens_type_then_key_then_file(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"bot/.env": f"OPENROUTER_API_KEY={OPENROUTER}\n", "notes.md": f"{ANTHROPIC}\n{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    rows = screen(explorer)
    assert "High 2" in rows[1] and any(row.strip().startswith("› ▸ openrouter") for row in rows)
    press(explorer, "enter", "down", "right")
    rows = "\n".join(screen(explorer))
    assert f"{peek(OPENROUTER)} ({len(OPENROUTER)} chars" in rows and "2 files" in rows
    assert "project/bot/.env" in rows and "project/notes.md" in rows and OPENROUTER not in rows


def test_a_key_in_many_files_groups_them_by_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    files = {f"editor/history/{index}/snapshot": f"{OPENROUTER}\n" for index in range(8)} | {"bot/.env": f"{OPENROUTER}\n"}
    folder = saved_scan(tmp_path, files)
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "right", "down", "right")
    labels = [row.label for row in explorer.rows()]
    assert labels[2:] == ["~/project/editor/history", "~/project/bot/.env"]
    press(explorer, "down", "right")
    assert sum("history/" in row.label for row in explorer.rows()) == 8


def test_tab_switches_tiers_and_the_filter_narrows(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"a/.env": f"FRED_API_KEY={FRED}\n", "b/notes.md": f"{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    assert [row.label for row in explorer.rows()] == ["openrouter"]
    press(explorer, "tab")
    assert [row.label for row in explorer.rows()] == ["fred"]
    press(explorer, "tab", "tab", "/", *"notes", "enter")
    assert [row.label for row in explorer.rows()] == ["openrouter"] and "filter: notes" in screen(explorer)[1]


def test_the_file_view_hides_the_key_until_asked_and_hides_it_again(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": "# bot\nLOG_LEVEL=info\nOPENROUTER_API_KEY=" + OPENROUTER + "\nREGION=eu\n"})
    clock = Clock()
    explorer = Explorer(load_scan(folder), folder, clock)
    press(explorer, "right", "down", "right", "down", "enter")
    rows = "\n".join(screen(explorer))
    assert "LOG_LEVEL=info" in rows and f"OPENROUTER_API_KEY={peek(OPENROUTER)}" in rows and OPENROUTER not in rows
    press(explorer, "s")
    rows = "\n".join(screen(explorer))
    assert OPENROUTER in rows and f"hides in {REVEAL_SECONDS}s" in rows
    clock.now += REVEAL_SECONDS
    assert OPENROUTER not in "\n".join(screen(explorer))
    press(explorer, "s", "escape")
    assert OPENROUTER not in "\n".join(screen(explorer)) and explorer.file_view is None


def test_a_long_line_starts_near_the_key_and_other_keys_stay_hidden(tmp_path: Path) -> None:
    line = '{"content": "' + "x " * 30_000 + f"use {ANTHROPIC} then {OPENROUTER} " + "y " * 30_000 + '"}'
    folder = saved_scan(tmp_path, {"transcript.jsonl": line + "\n"})
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "right", "down", "right", "down", "enter")
    rows = screen(explorer, width=100)
    [key_row] = [row for row in rows if "│" in row and "…" in row]
    assert peek(ANTHROPIC) in key_row or peek(OPENROUTER) in key_row
    assert ANTHROPIC not in key_row and OPENROUTER not in key_row and len(key_row) <= 100


def test_a_file_that_changed_since_the_scan_says_so(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"})
    (tmp_path / "project" / ".env").write_text("# moved to envh\n")
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "right", "down", "right", "down", "enter")
    assert any("isn't in the file any more" in row for row in screen(explorer))


def test_control_characters_in_a_file_cannot_restyle_the_screen(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"\x1b[2J\x1b]0;title\x07{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "right", "down", "right", "down", "enter")
    raw = "\n".join(explorer.render(120, 30))
    assert "\x1b[2J" not in raw and "\x1b]0" not in raw and "\x07" not in raw


def test_keys_are_read_from_escape_sequences_and_pastes() -> None:
    assert split_keys("\x1b[A\x1b[B\x1b[C\x1b[D\t\x1b[Z\r/x") == ["up", "down", "right", "left", "tab", "shift_tab", "enter", "/", "x"]
    assert split_keys("\x1b[200~ab\ncd\x1b[201~") == ["a", "b", "c", "d"]


def test_the_newest_scan_is_opened_by_default_and_old_ones_are_refused(tmp_path: Path) -> None:
    first = saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"})
    newer = first.parent / "20261018-090000"
    newer.mkdir()
    (newer / "hits.json").write_text('{"scanned": 1, "hits": [{"path": "/x", "line": 1, "kind": "openai", "name": "", "prefix": "sk-p", "fingerprint": "ab", "length": 9}]}')
    assert newest_scan(first.parent) == newer
    with pytest.raises(ClientError, match="older envh scan"):
        load_scan(newer)
    with pytest.raises(ClientError, match="no saved scan yet"):
        newest_scan(tmp_path / "nothing")


def test_explore_needs_a_terminal(tmp_path: Path) -> None:
    with pytest.raises(ClientError, match="run it in a terminal"):
        explore(saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"}))


ROW_WRITE = re.compile(r"\x1b\[(\d+);1H\x1b\[2K")


def last_screen(transcript: str, rows: int) -> list[str]:
    shown = [""] * rows
    parts = ROW_WRITE.split(transcript.split("\x1b[2J")[-1])
    for number, text in zip(parts[1::2], parts[2::2]):
        if 0 < int(number) <= rows:
            shown[int(number) - 1] = ANSI.sub("", text)
    return shown


def test_the_real_view_on_a_terminal_shows_the_key_only_on_request(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"OPENROUTER_API_KEY={OPENROUTER}\n"})
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 110, 0, 0))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HOME": str(tmp_path)}
    process = subprocess.Popen([sys.executable, "-m", "envh", "explore", str(folder)], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    output = bytearray()

    def wait_for(text: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if text in ANSI.sub("", output.decode(errors="replace")):
                return
            if select.select([master], [], [], 0.1)[0]:
                output.extend(os.read(master, 65536))
        raise AssertionError(f"{text!r} never appeared")

    try:
        wait_for("Enter view")
        for keys, then in ((b"\r", "1 key in 1 file"), (b"\x1b[B\x1b[C", "1 file"), (b"\x1b[B\r", "s show the key")):
            os.write(master, keys)
            wait_for(then)
        time.sleep(0.3)
        while select.select([master], [], [], 0.2)[0]:
            output.extend(os.read(master, 65536))
        assert OPENROUTER not in "\n".join(last_screen(output.decode(errors="replace"), 24))
        os.write(master, b"s")
        wait_for("The key is showing")
        assert any(OPENROUTER in row for row in last_screen(output.decode(errors="replace"), 24))
        os.write(master, b"\x1b")
        wait_for("Enter view")
        os.write(master, b"q")
        assert process.wait(timeout=10) == 0
        while select.select([master], [], [], 0.2)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break
        assert output.decode(errors="replace").rstrip().endswith("\x1b[?1049l")
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
