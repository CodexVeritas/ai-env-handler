import os
import stat
import time
from pathlib import Path

import pytest

from envh.common import value_id
import envh.tools.replacer as replacer_module
from envh.tools.replacer import Spot, placeholder, replace_all, replace_in_file, replaced_pairs
from envh.tools.scanner import scan_paths, scan_text
from tests.conftest import HEX, random_text

OPENROUTER = "sk-or-v1-" + random_text("openrouter", 64, HEX)
FRED = random_text("fred", 32, HEX)
AN_HOUR_AGO = time.time() - 3600


def old_file(path: Path, data: bytes, mode: int = 0o640) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    os.utime(path, (AN_HOUR_AGO, AN_HOUR_AGO))
    return path


def test_only_the_chosen_keys_change_and_every_other_byte_stays(tmp_path: Path) -> None:
    other = "sk-or-v1-" + random_text("other", 64, HEX)
    original = f"# keys\xff\nOPENROUTER_API_KEY={OPENROUTER}\r\nFRED_API_KEY={FRED}\nKEEP={other}\n".encode("utf-8", errors="surrogateescape")
    original = b"# keys \xff\n" + original.split(b"\n", 1)[1]
    file = old_file(tmp_path / ".env", original)
    count, problem, _ = replace_in_file(file, {value_id(OPENROUTER), value_id(FRED)}, time.time())
    assert (count, problem) == (2, "")
    expected = original.replace(OPENROUTER.encode(), placeholder("openrouter", value_id(OPENROUTER)).encode()).replace(FRED.encode(), placeholder("fred", value_id(FRED)).encode())
    assert file.read_bytes() == expected and stat.S_IMODE(file.stat().st_mode) == 0o640
    assert [hit.kind for hit in scan_text(file, file.read_text(errors="replace"))] == ["openrouter"]


def test_a_file_changed_recently_a_link_and_a_moved_key_are_left_alone(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh.jsonl"
    fresh.write_text(f"{OPENROUTER}\n")
    link = tmp_path / "link"
    link.symlink_to(fresh)
    moved = old_file(tmp_path / "moved.env", b"# moved to envh\n")
    now = time.time()
    assert replace_in_file(fresh, {value_id(OPENROUTER)}, now)[:2] == (0, "changed in the last 10 minutes; it may be in use")
    assert replace_in_file(link, {value_id(OPENROUTER)}, now)[:2] == (0, "a link")
    assert replace_in_file(moved, {value_id(OPENROUTER)}, now)[:2] == (0, "the key isn't in it any more")
    assert fresh.read_text() == f"{OPENROUTER}\n"


def test_a_private_key_is_left_for_the_person(tmp_path: Path) -> None:
    file = old_file(tmp_path / "id_rsa", f"-----BEGIN RSA PRIVATE KEY-----\n{random_text('pem', 64)}\n".encode())
    [hit] = scan_paths([file], 1 << 20)[0]
    assert replace_in_file(file, {hit.fingerprint}, time.time())[:2] == (0, "the key isn't in it any more")


def test_replace_all_logs_paths_and_ids_never_values(tmp_path: Path) -> None:
    first = old_file(tmp_path / "a" / ".env", f"KEY={OPENROUTER}\n".encode())
    second = old_file(tmp_path / "b" / "notes.md", f"{OPENROUTER} and {OPENROUTER}\n".encode())
    outcome = replace_all({str(first): {value_id(OPENROUTER)}, str(second): {value_id(OPENROUTER)}, str(tmp_path / "gone"): {value_id(OPENROUTER)}}, tmp_path)
    assert outcome.copies == 3 and outcome.replaced == {str(first): 1, str(second): 2} and "can't read it" in outcome.skipped[str(tmp_path / "gone")]
    log = (tmp_path / "replaced.txt").read_text()
    assert str(first) in log and value_id(OPENROUTER) in log and OPENROUTER not in log
    assert scan_paths([tmp_path / "a", tmp_path / "b"], 1 << 20)[0] == []


def test_a_credential_file_an_app_signs_in_with_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    hosts = old_file(tmp_path / ".config" / "gh" / "hosts.yml", f"github.com:\n    oauth_token: {OPENROUTER}\n".encode())
    assert replace_in_file(hosts, {value_id(OPENROUTER)}, time.time())[:2] == (0, "an app signs in with it, so replacing the key would sign you out")
    assert OPENROUTER in hosts.read_text()


def test_a_rewritten_file_keeps_its_timestamp_so_a_second_replace_isnt_skipped(tmp_path: Path) -> None:
    other = "sk-or-v1-" + random_text("second", 64, HEX)
    file = old_file(tmp_path / "History" / "snap", f"A={OPENROUTER}\nB={other}\n".encode())
    assert replace_in_file(file, {value_id(OPENROUTER)}, time.time())[:2] == (1, "")
    assert abs(file.stat().st_mtime - AN_HOUR_AGO) < 1
    assert replace_in_file(file, {value_id(other)}, time.time())[:2] == (1, "")


def test_the_log_lists_what_was_replaced(tmp_path: Path) -> None:
    file = old_file(tmp_path / "a" / ".env", f"KEY={OPENROUTER}\n".encode())
    replace_all({str(file): {value_id(OPENROUTER)}}, tmp_path)
    assert replaced_pairs(tmp_path) == {(str(file), value_id(OPENROUTER))} and replaced_pairs(tmp_path / "none") == set()


def test_a_copy_found_only_by_the_vault_search_is_replaced_where_the_scan_saw_it(tmp_path: Path) -> None:
    file = old_file(tmp_path / "notes.md", f"my fred one: {FRED}\nagain {FRED} here\n".encode())
    spots = [Spot(1, 13, len(FRED), value_id(FRED), "fred"), Spot(2, 6, len(FRED), value_id(FRED), "fred")]
    assert replace_in_file(file, {value_id(FRED)}, time.time())[:2] == (0, "the key isn't in it any more")
    assert replace_in_file(file, {value_id(FRED)}, time.time(), spots)[:2] == (2, "")
    removed = placeholder("fred", value_id(FRED))
    assert file.read_text() == f"my fred one: {removed}\nagain {removed} here\n"
    assert replace_in_file(file, {value_id(FRED)}, time.time(), spots)[:2] == (0, "the key isn't in it any more")


def test_only_the_ids_actually_replaced_in_a_file_are_logged(tmp_path: Path) -> None:
    file = old_file(tmp_path / "notes.md", f"FRED_API_KEY={FRED}\nmy other one: {OPENROUTER[9:]}\n".encode())
    moved = Spot(2, 0, len(OPENROUTER) - 9, value_id(OPENROUTER[9:]), "unknown")
    outcome = replace_all({str(file): {value_id(FRED), value_id(OPENROUTER[9:])}}, tmp_path, {str(file): [moved]})
    assert outcome.ids == {str(file): frozenset({value_id(FRED)})}
    assert replaced_pairs(tmp_path) == {(str(file), value_id(FRED))}


def test_overlapping_copies_are_replaced_once_and_the_line_stays_whole(tmp_path: Path) -> None:
    key = random_text("overlap", 24)
    file = old_file(tmp_path / "app.py", f"API_KEY={key}/v2/extra\n".encode())
    longer = f"{key}/v2/extra"
    assert [hit.fingerprint for hit in scan_text(file, file.read_text())] == [value_id(longer)]
    count, _, _ = replace_in_file(file, {value_id(key), value_id(longer)}, time.time(), [Spot(1, 8, len(key), value_id(key), "unknown")])
    assert count == 1 and file.read_text() == f"API_KEY={placeholder('unknown', value_id(key))}/v2/extra\n"


def test_a_file_too_large_to_rewrite_is_left_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = old_file(tmp_path / "server.log", f"boot {OPENROUTER}\n".encode() + b"filler\n" * 100)
    monkeypatch.setattr(replacer_module, "WHOLE_FILE_MAX_BYTES", 100)
    assert replace_in_file(file, {value_id(OPENROUTER)}, time.time())[:2] == (0, "over 0 MB, too large to rewrite here; replace the key by hand")
