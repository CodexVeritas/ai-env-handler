import json
from pathlib import Path

import pytest

from envh.core import audit
from envh.core.audit import AuditTail, read_events


def write_lines(path: Path, count: int) -> None:
    with path.open("w") as handle:
        for number in range(count):
            handle.write(json.dumps({"ts": "2026-10-01T10:00:00", "event": "admin_add", "secret": f"KEY_{number}", "padding": "x" * (number % 37)}) + "\n")


def secrets(path: Path, limit: int) -> tuple[list[str], bool]:
    tail = read_events(path, limit)
    assert tail.unreadable == 0
    return [event.fields["secret"] for event in tail.events], tail.older


@pytest.mark.parametrize("block", [7, 64, 1 << 16])
@pytest.mark.parametrize("limit", [0, 1, 5, 300, 599, 600, 1000])
def test_reading_back_from_the_end_returns_exactly_the_last_lines_and_says_if_there_are_more(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: int, limit: int
) -> None:
    monkeypatch.setattr(audit, "TAIL_BLOCK_BYTES", block)
    path = tmp_path / "audit.jsonl"
    write_lines(path, 600)
    assert secrets(path, limit) == ([f"KEY_{number}" for number in range(max(600 - limit, 0), 600)], limit < 600)


def test_a_torn_last_line_damaged_bytes_and_garbage_are_counted_not_dropped_silently(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_lines(path, 3)
    with path.open("ab") as handle:
        handle.write(b'{"ts": "2026-10-01T10:00:00", "event": "admin_add", "secret": "KEY_\xff"}\n')
        handle.write(b'["not", "an", "event"]\n{"ts": "2026-10-01T10:00:00", "event": "adm')
    tail = read_events(path, 10)
    assert [event.fields["secret"] for event in tail.events] == ["KEY_0", "KEY_1", "KEY_2"] and tail.unreadable == 3


def test_an_empty_file_has_no_events(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.touch()
    assert read_events(path, 10) == AuditTail([], 0, False)
