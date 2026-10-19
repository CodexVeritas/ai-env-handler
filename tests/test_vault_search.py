import argparse
import asyncio
import json
from pathlib import Path

import pytest

from envh.common import value_id
from envh.tools.saved_files import VAULT_FILE
from envh.tools.scanner import main
from envh.tools.vault_search import confirm, vault_search
from tests.conftest import HEX, Harness, approve_next, random_text

FRED = random_text("stored fred", 32, HEX)


def scan_args(tmp_path: Path) -> argparse.Namespace:
    home = tmp_path / "home"
    home.mkdir()
    (home / "notes.md").write_text(f"my fred key: {FRED}\nFRED_API_KEY={FRED}\n")
    return argparse.Namespace(paths=[str(home)], json=False, max_size=25, out=str(tmp_path / "scans"))


def saved_hits(folder: Path) -> list[tuple[int, str, str]]:
    return [(hit["line"], hit["kind"], hit["confidence"]) for hit in json.loads((folder / "hits.json").read_text())["hits"]]


async def test_one_approval_finds_copies_of_stored_keys_and_marks_them_in_the_saved_scan(
    control: Path, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.broker.vault.set("NEWS_BOT_FRED_API_KEY", FRED)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    scan = asyncio.ensure_future(asyncio.to_thread(main, scan_args(tmp_path), lambda: vault_search(control)))
    await approve_next(harness)
    exit_code, folder = await scan
    assert exit_code == 1 and folder is not None
    assert saved_hits(folder) == [(1, "fred", "high"), (2, "fred", "high")]
    assert json.loads((folder / VAULT_FILE).read_text())["matches"] == {value_id(FRED): ["NEWS_BOT_FRED_API_KEY"]}
    assert not any(FRED in (folder / name).read_text() for name in ("hits.json", "report.txt", VAULT_FILE))
    shown = capsys.readouterr().out
    assert "Searching for copies of 1 vault key too." in shown
    assert "Searched for 1 vault key: 1 stored key found, 1 copy only by that search." in shown
    assert [request.kind for request in harness.broker.state.requests.values()] == ["search"]


async def test_a_denied_search_scans_without_the_vault(
    control: Path, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness.broker.vault.set("NEWS_BOT_FRED_API_KEY", FRED)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    scan = asyncio.ensure_future(asyncio.to_thread(main, scan_args(tmp_path), lambda: vault_search(control)))
    for _ in range(50):
        if harness.broker.state.pending():
            break
        await asyncio.sleep(0.01)
    harness.broker.deny(harness.broker.state.pending()[0])
    _, folder = await scan
    assert saved_hits(folder) == [(2, "fred", "medium")] and not (folder / VAULT_FILE).exists()
    assert "Not searching for your vault's keys: denied" in capsys.readouterr().err


async def test_saying_no_or_no_broker_scans_without_the_vault(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = scan_args(tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert saved_hits(main(args, lambda: vault_search(tmp_path / "no.sock"))[1]) == [(2, "fred", "medium")]
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    args.out = str(tmp_path / "second")
    assert saved_hits(main(args, lambda: vault_search(tmp_path / "no.sock"))[1]) == [(2, "fred", "medium")]
    assert "Not searching for your vault's keys: broker not running" in capsys.readouterr().err


class BrokenReply:
    def send(self, **message: object) -> None:
        pass

    def recv_ok(self) -> dict[str, object]:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_a_malformed_answer_after_the_scan_is_reported_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    assert confirm(BrokenReply(), ["abcd1234"]) is None
    assert "couldn't check the found keys against your vault: Expecting value" in capsys.readouterr().err
