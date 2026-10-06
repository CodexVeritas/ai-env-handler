"""What happened, for the console's home screen: audit events turned into one short line each. Every event still goes
to audit.jsonl in full; the few that only repeat another line are left out here."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from envh.core.audit import AuditEvent

ACTIVITY_KEPT = 500
NO_REASON = "(no reason given)"
KIND_NAMES = {"session": "Session request", "run": "Run request", "preset": "Preset proposal", "import": "Import request"}
UNSHOWN = {"reload", "broker_start", "broker_stop", "run_auto_approved", "client_connection_lost"}


@dataclass(frozen=True)
class Entry:
    at: datetime
    kind: str
    text: str


def names(value: Any) -> str:
    return ", ".join(str(item) for item in value) if isinstance(value, (list, tuple)) else str(value)


def describe(event: AuditEvent) -> tuple[str, str] | None:
    """The kind (ok, warn, error or info) and text of an event's line, or None for one not shown."""
    fields = event.fields
    if event.name in UNSHOWN:
        return None
    if event.name == "request":
        presets = names(fields.get("presets") or [])
        reason = fields.get("reason")
        text = f"{KIND_NAMES.get(str(fields.get('kind')), 'Request')} #{fields.get('id')}" + (f" · {presets}" if presets else "")
        text += f' · "{reason}"' if reason and reason != NO_REASON else " · no reason given"
        return ("ok", f"{text} · granted, its keys need no approval") if fields.get("approval") == "auto" else ("warn", text)
    if event.name == "decision" and fields.get("by") == "auto":
        return None
    if event.name == "decision":
        approved = fields.get("outcome") == "approved"
        return ("ok", f"Approved #{fields.get('id')}") if approved else ("error", f"Denied #{fields.get('id')}")
    simple = {
        "session_start": ("ok", lambda: f"Session {str(fields.get('session'))[:8]} open until {str(fields.get('expires'))[11:16]} · {names(fields.get('vars', []))}"),
        "session_end": ("info", lambda: f"Session {str(fields.get('session'))[:8]} ended" + (" from the console" if fields.get("by") == "console" else "")),
        "run_start": ("info", lambda: f"Run #{fields.get('run')} started · {fields.get('command')}" + (f" · session {str(fields.get('session'))[:8]}" if fields.get("session") else "")),
        "run_end": ("info", lambda: f"Run #{fields.get('run')} ended · exit {fields.get('exit_code')}" + lingering(fields.get("lingering_terminated"))),
        "withdrawn": ("info", lambda: f"#{fields.get('id')} withdrawn: the program that asked went away"),
        "detached": ("info", lambda: f"#{fields.get('id')} still waits; the program that asked stopped waiting for it"),
        "abandoned": ("info", lambda: f"#{fields.get('id')} expired unanswered"),
        "wrong_passphrase": ("warn", lambda: f"Wrong passphrase ({fields.get('action')})"),
        "admin_add": ("ok", lambda: f"Stored {fields.get('secret')}"),
        "admin_replace": ("ok", lambda: f"Replaced the value of {fields.get('secret')}"),
        "admin_rm": ("ok", lambda: f"Removed {fields.get('secret')}"),
        "secret_revealed": ("warn", lambda: f"Copied {fields.get('secret')} to the clipboard" if fields.get("how") == "copied" else f"Showed the value of {fields.get('secret')}"),
        "secret_renamed": ("ok", lambda: f"Renamed {fields.get('old')} to {fields.get('new')}"),
        "presets_saved": ("ok", lambda: f"Saved presets: {names(fields.get('presets', []))}"),
        "presets_updated": ("ok", lambda: f"Presets updated: {names(fields.get('presets', []))}"),
        "import_applied": ("ok", lambda: f"Imported {len(fields.get('added') or [])} new key(s)" + (f" · presets {names(fields['presets'])}" if fields.get("presets") else "")),
        "settings_saved": ("ok", lambda: f"Saved settings: {names(fields.get('changed') or [])}"),
        "config_edited": ("ok", lambda: f"Saved {fields.get('file')}"),
        "vault_passphrase_changed": ("ok", lambda: "Changed the vault passphrase"),
        "rejected_uid": ("warn", lambda: f"Refused uid {fields.get('uid')}: not an allowed user (see /settings)"),
        "error": ("error", lambda: f"Internal error in the {fields.get('where', 'broker')}; details in audit.jsonl"),
    }
    if event.name in simple:
        kind, text = simple[event.name]
        return kind, text()
    return "info", event.line().split("] ", 1)[-1]


def lingering(count: Any) -> str:
    return f" · stopped {count} leftover process(es)" if isinstance(count, int) and count > 0 else ""


class Activity:
    """The newest lines of activity. on_change, when set, hears about each new line."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self.entries: deque[Entry] = deque(maxlen=ACTIVITY_KEPT)
        self.clock = clock
        self.on_change: Callable[[], None] | None = None

    def add(self, event: AuditEvent) -> None:
        described = describe(event)
        if described is not None:
            self._append(Entry(event.at, *described))

    def note(self, kind: str, text: str) -> None:
        self._append(Entry(self.clock(), kind, text))

    def _append(self, entry: Entry) -> None:
        self.entries.append(entry)
        if self.on_change is not None:
            self.on_change()
