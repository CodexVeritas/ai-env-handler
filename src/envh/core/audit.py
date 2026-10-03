"""Append-only audit log (JSON lines) that also echoes one line per event to the console."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from envh.common import printable

AUDIT_FILE = "audit.jsonl"


class Audit:
    def __init__(self, path: Path, echo: Callable[[str], None], clock: Callable[[], datetime]) -> None:
        self._path = path
        self._echo = echo
        self._clock = clock

    def event(self, event_name: str, /, **fields: Any) -> None:
        now = self._clock()
        record = {"ts": now.isoformat(timespec="seconds"), "event": event_name, **fields}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        details = " ".join(f"{key}={_short(value)}" for key, value in fields.items())
        self._echo(f"[{now:%H:%M:%S}] {event_name} {details}".rstrip())


def _short(value: Any) -> str:
    text = printable(value if isinstance(value, str) else json.dumps(value, default=str))
    return text if len(text) <= 80 else text[:77] + "..."
