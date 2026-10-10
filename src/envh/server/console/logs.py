"""The Logs screen: the audit log of this and earlier runs of the console, newest at the bottom, each line in full. New
events appear while it is open; scrolled back, the screen stays where it is."""

from __future__ import annotations

from typing import TYPE_CHECKING

from envh.core.audit import AUDIT_FILE, AuditEvent, read_events
from envh.server.console.activity import Entry, describe, transcript
from envh.server.console.dialogs import View
from envh.server.console.summaries import plural
from envh.server.console.tui import DIM, Key, Line, mark, scrolled, styled

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

LOGS_KEPT = 2000
PAGE = 10
MARKERS = {"broker_start": "Console started", "broker_stop": "Console stopped"}


def log_entry(event: AuditEvent) -> Entry | None:
    if event.name in MARKERS:
        return Entry(event.at, "info", MARKERS[event.name])
    described = describe(event)
    return Entry(event.at, *described) if described else None


class LogsView(View):
    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(app)
        self.path = app.broker.data_dir / AUDIT_FILE
        self.entries: list[Entry] = []
        self.loaded_size = -1
        self.unreadable = 0
        self.capped = False
        self.problem = ""
        self.offset = 0
        self.below = 0
        self.following = True

    def title(self) -> str:
        return "Logs"

    def hints(self) -> str:
        return "↑↓ PgUp PgDn scroll · Home oldest · End newest · Esc back"

    def handle(self, key: Key) -> None:
        if key == "home":
            self.offset, self.following = 0, False
        elif key == "end":
            self.following = True
        elif key in ("up", "down", "page_up", "page_down"):
            self.offset += {"up": -1, "down": 1, "page_up": -PAGE, "page_down": PAGE}[key]
            self.following = False
        else:
            super().handle(key)

    def status(self) -> Line | None:
        if self.problem:
            return mark("error", self.problem)
        if self.unreadable:
            return mark("warn", f"{plural(self.unreadable, 'line')} of audit.jsonl could not be read; the rest are shown.")
        if self.capped and self.offset == 0:
            return styled(f"Only the latest {LOGS_KEPT} events are shown; older ones stay in audit.jsonl.", DIM)
        return styled("↓ newer events below · End shows the latest", DIM) if self.below else None

    def body(self, columns: int, rows: int) -> list[Line]:
        self._load()
        if not self.entries:
            return [styled("  Nothing logged yet.", DIM)]
        lines = transcript(self.entries, columns)
        if self.following:
            self.offset = len(lines)
        visible, self.offset, self.below = scrolled(lines, rows, self.offset)
        self.following = not self.below
        if self.offset:
            visible = [styled(f"  ↑ {plural(self.offset + 1, 'earlier line')} · PgUp", DIM), *visible[1:]]
        return visible

    def _load(self) -> None:
        """Read the audit file again when it has grown."""
        try:
            size = self.path.stat().st_size if self.path.exists() else 0
            if size == self.loaded_size:
                return
            events, self.unreadable = read_events(self.path, LOGS_KEPT) if size else ([], 0)
        except OSError as error:
            self.problem = f"Could not read audit.jsonl: {error}"
            return
        self.problem = ""
        self.loaded_size = size
        self.capped = len(events) + self.unreadable >= LOGS_KEPT
        self.entries = [entry for entry in map(log_entry, events) if entry is not None]
