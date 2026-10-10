"""Append-only audit log (JSON lines). Each event also goes to a listener, which the console shows as activity."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from envh.common import printable

AUDIT_FILE = "audit.jsonl"
TAIL_BLOCK_BYTES = 1 << 16


@dataclass(frozen=True)
class AuditEvent:
    name: str
    fields: dict[str, Any]
    at: datetime

    def line(self) -> str:
        details = " ".join(f"{key}={_short(value)}" for key, value in self.fields.items())
        return f"[{self.at:%H:%M:%S}] {self.name} {details}".rstrip()


class Audit:
    def __init__(self, path: Path, listener: Callable[[AuditEvent], None], clock: Callable[[], datetime]) -> None:
        self._path = path
        self._listener = listener
        self._clock = clock

    def event(self, event_name: str, /, **fields: Any) -> None:
        now = self._clock()
        record = {"ts": now.isoformat(timespec="seconds"), "event": event_name, **fields}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._listener(AuditEvent(event_name, fields, now))


def read_events(path: Path, limit: int) -> tuple[list[AuditEvent], int]:
    """The events on the last limit lines of an audit file, and how many of those lines could not be read. It reads back
    from the end, so the time it takes does not grow with the file."""
    with path.open("rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        data = b""
        while position > 0 and data.count(b"\n") <= limit:
            step = min(TAIL_BLOCK_BYTES, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
    lines = data.splitlines()[1 if position > 0 else 0:]
    events: list[AuditEvent] = []
    unreadable = 0
    for line in lines[-limit:]:
        try:
            events.append(_parse(line.decode("utf-8", errors="replace")))
        except (ValueError, KeyError, TypeError, AttributeError):
            unreadable += 1
    return events, unreadable


def _parse(line: str) -> AuditEvent:
    record = json.loads(line)
    fields = {key: value for key, value in record.items() if key not in ("ts", "event")}
    return AuditEvent(str(record["event"]), fields, datetime.fromisoformat(record["ts"]))


def _short(value: Any) -> str:
    text = printable(value if isinstance(value, str) else json.dumps(value, default=str))
    return text if len(text) <= 80 else text[:77] + "..."
