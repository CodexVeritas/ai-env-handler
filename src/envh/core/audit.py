"""Append-only audit log (JSON lines). Each event also goes to a listener, which the console shows as activity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from envh.common import printable

AUDIT_FILE = "audit.jsonl"


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
        self._listener(AuditEvent(event_name, fields, now))


def _short(value: Any) -> str:
    text = printable(value if isinstance(value, str) else json.dumps(value, default=str))
    return text if len(text) <= 80 else text[:77] + "..."
