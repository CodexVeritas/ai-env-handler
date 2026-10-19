import fcntl
import json
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
from envh.tools.explorer import ANSI, REVEAL_SECONDS, Explorer, explore, load_scan, load_vault_matches, main, newest_scan, split_keys
from envh.common import peek_ends, value_id
from envh.tools.scanner import KeyEnds, scan_paths, scan_text, save_results, shown_value, with_vault_copies
from tests.conftest import HEX, random_text

ROOT = Path(__file__).resolve().parents[1]
OPENROUTER = "sk-or-v1-" + random_text("openrouter", 64, HEX)
ANTHROPIC = "sk-ant-api03-" + random_text("anthropic", 48)
FRED = random_text("fred", 32, HEX)


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


def choose(explorer: Explorer, kind: str) -> None:
    explorer.selected = next(index for index, row in enumerate(explorer.rows()) if row.node[0] == kind)


def open_first_file(explorer: Explorer) -> None:
    press(explorer, "right")
    choose(explorer, "file")
    press(explorer, "enter")


def shown(key: str) -> str:
    return shown_value(key, len("sk-or-v1-") if key.startswith("sk-or-v1-") else len("sk-ant-"))


def test_the_tree_opens_type_then_key_then_file(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"bot/.env": f"OPENROUTER_API_KEY={OPENROUTER}\n", "notes.md": f"{ANTHROPIC}\n{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    rows = screen(explorer)
    assert "Real key?  Likely 2" in rows[1] and any(row.strip().startswith("› ▸ openrouter") and "→ shows its keys" in row for row in rows)
    press(explorer, "enter")
    rows = "\n".join(screen(explorer))
    assert f"{shown(OPENROUTER)} ({len(OPENROUTER)} chars" in rows and "2 files" in rows
    assert "project/bot/.env" in rows and "project/notes.md" in rows and OPENROUTER not in rows
    choose(explorer, "file")
    assert any("Enter shows the lines around the key" in row for row in screen(explorer, width=220))


def test_a_key_in_many_files_groups_them_by_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    files = {f"editor/history/{index}/snapshot": f"{OPENROUTER}\n" for index in range(8)} | {"bot/.env": f"{OPENROUTER}\n"}
    folder = saved_scan(tmp_path, files)
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "right")
    labels = [row.label for row in explorer.rows()]
    assert labels[2:] == ["~/project/editor/history", "~/project/bot/.env"]
    choose(explorer, "folder")
    assert any("→ shows these files" in row for row in screen(explorer))
    press(explorer, "right")
    assert sum("history/" in row.label for row in explorer.rows()) == 8


def test_tab_switches_tiers_and_the_filter_narrows(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"a/.env": f"FRED_API_KEY={FRED}\n", "b/notes.md": f"{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    assert [row.label for row in explorer.rows()] == ["openrouter"]
    press(explorer, "tab")
    assert [row.label for row in explorer.rows()] == ["fred"]
    press(explorer, "tab", "tab", "tab", "/", *"notes", "enter")
    assert [row.label for row in explorer.rows()] == ["openrouter"] and "filter: notes" in screen(explorer)[2]


def test_the_file_view_hides_the_key_until_asked_and_hides_it_again(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": "# bot\nLOG_LEVEL=info\nOPENROUTER_API_KEY=" + OPENROUTER + "\nREGION=eu\n"})
    clock = Clock()
    explorer = Explorer(load_scan(folder), folder, clock)
    open_first_file(explorer)
    rows = "\n".join(screen(explorer))
    assert "LOG_LEVEL=info" in rows and f"OPENROUTER_API_KEY=sk-or-v1-{OPENROUTER[9:17]}…{OPENROUTER[-4:]}" in rows and OPENROUTER not in rows
    assert "Press s to show the full key." in rows
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
    open_first_file(explorer)
    rows = screen(explorer, width=100)
    [key_row] = [row for row in rows if "│" in row and "…" in row]
    assert f"sk-ant-{ANTHROPIC[7:15]}…{ANTHROPIC[-4:]}" in key_row
    assert ANTHROPIC not in key_row and OPENROUTER not in key_row and len(key_row) <= 100


def test_a_file_that_changed_since_the_scan_says_so(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"})
    (tmp_path / "project" / ".env").write_text("# moved to envh\n")
    explorer = Explorer(load_scan(folder), folder)
    open_first_file(explorer)
    assert any("isn't in the file any more" in row for row in screen(explorer))


def test_control_characters_in_a_file_cannot_restyle_the_screen(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"\x1b[2J\x1b]0;title\x07{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    open_first_file(explorer)
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
        explore(saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"}), tmp_path / "ctl.sock")


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
        wait_for("against your vault? [Y/n]")
        os.write(master, b"n\r")
        wait_for("x replace")
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
        wait_for("x replace")
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


def test_g_groups_by_place(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"app/.env": f"{OPENROUTER}\n", "app/README.md": f"{ANTHROPIC}\n{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "g")
    assert [row.label for row in explorer.rows()] == [".env files", "docs and notes"] and "Grouped by place" in screen(explorer)[2]
    press(explorer, "down", "right")
    assert sorted(row.label.split()[0] for row in explorer.rows() if row.node[0] == "value") == ["anthropic", "openrouter"]


def test_keys_in_the_vault_are_named_and_v_filters_them(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"a/.env": f"{OPENROUTER}\n", "b/notes.md": f"{ANTHROPIC}\n"})
    hits = load_scan(folder)
    stored = next(hit.fingerprint for hit in hits if hit.kind == "openrouter")
    (folder / "vault.json").write_text(json.dumps({"matches": {stored: ["TEAM_OPENROUTER_KEY"]}}))
    explorer = Explorer(hits, folder, vault=load_vault_matches(folder))
    press(explorer, "v")
    assert [row.label for row in explorer.rows()] == ["openrouter"] and "in your vault (v)" in screen(explorer)[2]
    press(explorer, "right")
    assert any("in your vault as TEAM_OPENROUTER_KEY" in row.label for row in explorer.rows())
    press(explorer, "v")
    assert [row.label for row in explorer.rows()] == ["anthropic"]
    assert load_vault_matches(tmp_path) is None


def test_v_without_a_vault_check_says_how_to_get_one(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"})
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "v")
    assert "Not checked against your vault" in screen(explorer)[-2] and "vault not checked" in screen(explorer)[2]


def test_the_file_view_shows_and_hides_copies_only_the_vault_search_found(tmp_path: Path) -> None:
    stored = random_text("vault only", 32, HEX)
    other = random_text("other stored", 24)
    notes = tmp_path / "project" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text(f"intro\nmy fred key: {stored}\nnext line has {other} too\n")
    vault = {value_id(stored): ["MY_FRED_KEY"], value_id(other): ["MY_OTHER_TOKEN"]}
    found = list(scan_text(notes, notes.read_text(), KeyEnds([(*peek_ends(value), len(value)) for value in (stored, other)])))
    folder = save_results(with_vault_copies(found, vault), 1, [], 1.0, [notes.parent], tmp_path / "scans", datetime(2026, 10, 17, 20, 0, 0))
    explorer = Explorer(load_scan(folder), folder, vault=vault)
    explorer.selected = next(index for index, row in enumerate(explorer.rows()) if row.label == "fred")
    open_first_file(explorer)
    rows = "\n".join(screen(explorer))
    assert "isn't in the file any more" not in rows and "my fred key:" in rows and "next line has" in rows
    assert stored not in rows and other not in rows


def test_the_file_view_finds_a_vault_copy_inside_a_longer_value_the_rescan_reports(tmp_path: Path) -> None:
    stored = random_text("inside", 24)
    app = tmp_path / "project" / "app.py"
    app.parent.mkdir(parents=True)
    app.write_text(f"API_KEY={stored}/v2/extra\n")
    vault = {value_id(stored): ["MY_TOKEN"]}
    found = list(scan_text(app, app.read_text(), KeyEnds([(*peek_ends(stored), len(stored))])))
    folder = save_results(with_vault_copies(found, vault), 1, [], 1.0, [app.parent], tmp_path / "scans", datetime(2026, 10, 17, 20, 0, 0))
    explorer = Explorer(load_scan(folder), folder, vault=vault)
    explorer.selected = next(index for index, row in enumerate(explorer.rows()) if row.node[0] == "top")
    open_first_file(explorer)
    rows = "\n".join(screen(explorer))
    assert "isn't in the file any more" not in rows and "API_KEY=" in rows and "/v2/extra" in rows and stored not in rows


def test_x_replaces_every_copy_under_a_row_after_y(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"a/.env": f"LOG_LEVEL=info\nOPENROUTER_API_KEY={OPENROUTER}\n", "b/notes.md": f"use {OPENROUTER} or {ANTHROPIC}\n"})
    for file in (tmp_path / "project").rglob("*"):
        os.utime(file, (time.time() - 3600, time.time() - 3600))
    explorer = Explorer(load_scan(folder), folder)
    choose(explorer, "top")
    press(explorer, "x", "n")
    assert explorer.message == "Nothing replaced." and OPENROUTER in (tmp_path / "project" / "a" / ".env").read_text()
    press(explorer, "x")
    rows = "\n".join(screen(explorer))
    assert "anthropic, likely keys: 1 copy of 1 key in 1 file" in rows or "openrouter, likely keys: 2 copies of 1 key in 2 files" in rows
    assert "Type y to replace them" in rows
    label = explorer.rows()[explorer.selected].label
    press(explorer, "y")
    explorer.work()
    assert explorer.message.startswith("✓ Replaced") and label not in [row.label for row in explorer.rows()]
    assert (folder / "replaced.txt").exists() and OPENROUTER not in (folder / "replaced.txt").read_text()


def test_the_replace_summary_warns_about_keys_outside_the_vault_and_low_values_in_code(tmp_path: Path) -> None:
    folder = saved_scan(tmp_path, {"a/.env": f"{OPENROUTER}\n{ANTHROPIC}\nGENERATION_KEY={FRED}\n"})
    hits = load_scan(folder)
    stored = next(hit.fingerprint for hit in hits if hit.kind == "openrouter")
    explorer = Explorer(hits, folder, vault={stored: ["TEAM_OPENROUTER_KEY"]})
    explorer.tier = "all"
    explorer.expand(explorer.rows()[0].node)
    choose(explorer, "file")
    press(explorer, "x")
    rows = "\n".join(screen(explorer))
    assert "isn't in your vault; once replaced it's gone" in rows or "aren't in your vault" in rows
    assert "Unlikely keys are mostly not keys at all" in rows
    press(explorer, "n")
    unchecked = Explorer(hits, folder)
    choose(unchecked, "top")
    press(unchecked, "x")
    assert "Not checked against your vault" in "\n".join(screen(unchecked))


def test_explore_given_a_folder_of_scans_opens_the_newest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = saved_scan(tmp_path, {".env": f"{OPENROUTER}\n"})
    opened: list[Path] = []
    monkeypatch.setattr("envh.tools.explorer.explore", lambda chosen, sock: opened.append(chosen) or 0)
    main(str(folder.parent), tmp_path / "ctl.sock")
    main(str(folder), tmp_path / "ctl.sock")
    assert opened == [folder, folder]


def test_all_shows_each_place_once_and_x_there_replaces_every_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    history = {f"project/.config/Code/User/History/-{index}/snap": f"OPENROUTER_API_KEY={OPENROUTER}\nFRED_API_KEY={FRED}\n" for index in range(3)}
    folder = saved_scan(tmp_path, history)
    for file in (tmp_path / "project").rglob("*"):
        os.utime(file, (time.time() - 3600, time.time() - 3600))
    explorer = Explorer(load_scan(folder), folder)
    press(explorer, "g")
    choose(explorer, "top")
    press(explorer, "x")
    rows = "\n".join(screen(explorer, width=160))
    assert "Only likely keys are replaced. 3 copies in other lists stay here:" in rows and "   3 possible." in rows and "Tab to All" in rows
    press(explorer, "escape")
    explorer.tier = "all"
    assert [row.label for row in explorer.rows()] == ["editor history"]
    choose(explorer, "top")
    press(explorer, "x", "y")
    explorer.work()
    assert explorer.message.startswith("✓ Replaced 6 copies in 3 files")
    assert all(OPENROUTER not in file.read_text() and FRED not in file.read_text() for file in (tmp_path / "project").rglob("snap"))
    assert load_scan(folder) == [] and explorer.rows() == []
