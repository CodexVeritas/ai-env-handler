import os
import stat
import time
from pathlib import Path

from envh.common import value_id
from envh.tools.replacer import placeholder, replace_all, replace_in_file
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
    count, problem = replace_in_file(file, {value_id(OPENROUTER), value_id(FRED)}, time.time())
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
    assert replace_in_file(fresh, {value_id(OPENROUTER)}, now) == (0, "changed in the last 10 minutes; it may be in use")
    assert replace_in_file(link, {value_id(OPENROUTER)}, now) == (0, "a link")
    assert replace_in_file(moved, {value_id(OPENROUTER)}, now) == (0, "the key isn't in it any more")
    assert fresh.read_text() == f"{OPENROUTER}\n"


def test_a_private_key_is_left_for_the_person(tmp_path: Path) -> None:
    file = old_file(tmp_path / "id_rsa", b"-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n")
    [hit] = scan_paths([file], 1 << 20)[0]
    assert replace_in_file(file, {hit.fingerprint}, time.time()) == (0, "the key isn't in it any more")


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
    assert replace_in_file(hosts, {value_id(OPENROUTER)}, time.time()) == (0, "an app signs in with it, so replacing the key would sign you out")
    assert OPENROUTER in hosts.read_text()
