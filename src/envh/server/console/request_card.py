"""The request card: one waiting request at a time, with everything needed to decide it, answered with the vault
passphrase. Every key goes to the card while it is shown, so a passphrase meant for it never lands anywhere else."""

from __future__ import annotations

import shlex
from datetime import timedelta
from typing import TYPE_CHECKING

from envh.core.broker import Broker
from envh.core.durations import format_duration
from envh.core.state import Request
from envh.server.console.summaries import login_name, plural, preset_changes, preset_drafts
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, GREEN, POINTER, YELLOW, Key, Line, Span, TextField, box, mark, scrolled, styled, wrap_line

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

LABEL_WIDTH = 10
PAGE = 8


def waited(duration: timedelta) -> str:
    seconds = max(int(duration.total_seconds()), 0)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02}:{rest % 60:02}" if hours else f"{rest // 60}:{rest % 60:02}"


def key_lines(mapping: dict[str, str], broker: Broker) -> list[Line]:
    width = max((len(var) for var in mapping), default=0)
    lines = []
    for var, secret in sorted(mapping.items()):
        description = broker.config.description_for(secret)
        lines.append([Span(f"{var:<{width}} ← {secret}"), *([Span(f"  {description}", DIM)] if description else [])])
    return lines


def import_lines(summary: dict) -> list[Line]:
    reused = summary.get("reused") or {}
    renamed = summary.get("renamed") or {}
    unchanged = set(summary.get("unchanged") or [])
    lines = []
    for name, shown in sorted((summary.get("fingerprints") or {}).items()):
        if name in reused:
            lines.append([Span("· ", DIM), Span(name), Span(f"  {shown}  same value already stored as {reused[name]}", DIM)])
        elif name in renamed:
            lines.append([Span("+ ", GREEN), Span(renamed[name]), Span(f"  {shown}  sent as {name}, which holds a different value", DIM)])
        elif name in unchanged:
            lines.append([Span("· ", DIM), Span(name), Span(f"  {shown}  already stored", DIM)])
        else:
            lines.append([Span("+ ", GREEN), Span(name), Span(f"  {shown}", DIM)])
    return lines


def content(request: Request, broker: Broker) -> tuple[str, str, list[tuple[str, list[Line]]]]:
    """The card's title, its one-line summary, and its labelled details. Text from the requester is shown in full, wrapped,
    never cut, since the end of a command line can matter most."""
    who = login_name(request.provenance.uid)
    presets = ", ".join(request.presets)
    reason = [styled(f'"{request.reason}"')] if request.reason else [styled("none given. Ask why before approving.", YELLOW)]
    rows: list[tuple[str, list[Line]]] = [("Reason", reason)]
    if request.kind == "session":
        granted = format_duration(request.granted or timedelta())
        title = f"Session request #{request.id}"
        summary = f"{who} asks for a {granted} session" + (f" with {presets}" if presets else "")
        rows.append(("Keys", key_lines(request.mapping, broker)))
        excluded = request.summary.get("excluded_per_run") or []
        if excluded:
            rows.append(("Left out", [styled(f"{', '.join(excluded)}: per-run, so every run asks you", DIM)]))
        capped = request.requested is not None and request.requested != request.granted
        rows.append(("Duration", [styled(granted + (f"  (asked for {format_duration(request.requested)}; key and preset limits cap it)" if capped else ""))]))
        rows.append(("Command", [styled(shlex.join(request.command))]))
    elif request.kind == "run":
        title = f"Run request #{request.id}"
        summary = f"{who} asks to run a command with {plural(len(request.mapping), 'key')}" + (f" from {presets}" if presets else "")
        rows.append(("Runs", [styled(shlex.join(request.command))]))
        rows.append(("Keys", key_lines(request.mapping, broker)))
        rows.append(("", [styled("The command can read these values while it runs.", DIM)]))
    elif request.kind == "preset":
        names = request.summary.get("presets") or []
        title = f"Preset proposal #{request.id}"
        summary = f"{who} proposes changes to {'preset' if len(names) == 1 else 'presets'} {', '.join(names)}"
        after = broker.merged_presets(request.summary.get("additions") or {})
        rows.append(("Changes", preset_changes(preset_drafts(broker.config.presets_raw), preset_drafts(after)) or [styled("none", DIM)]))
    else:
        title = f"Import request #{request.id}"
        added = request.summary.get("added") or []
        summary = f"{who} asks to store {plural(len(added), 'new key')}" + (f" and update {plural(len(request.summary.get('presets') or []), 'preset')}" if request.summary.get("presets") else "")
        rows.append(("Keys", import_lines(request.summary)))
        if request.summary.get("presets"):
            after = broker.merged_presets(request.summary.get("additions") or {})
            rows.append(("Presets", preset_changes(preset_drafts(broker.config.presets_raw), preset_drafts(after))))
    rows.append(("Process", [styled(f"pid {request.provenance.pid} · {request.provenance.cmdline}")]))
    return title, summary, rows


class RequestCard:
    """Typing goes to the hidden passphrase field and Enter approves; ↓ or Esc picks Deny instead, and Enter then denies.
    A withdrawn request stays on screen, swallowing keys, until Enter, so a passphrase being typed for it is not taken as
    anything else."""

    def __init__(self, app: ConsoleApp, request: Request) -> None:
        self.app = app
        self.request = request
        self.deny_chosen = False
        self.field = TextField(hidden=True, accept=lambda char: "" if char in "\r\n" else char)
        self.withdrawn = False
        self.problem: str | None = None
        self.offset = 0

    def hints(self) -> str:
        if self.withdrawn:
            return "Enter go on"
        if self.deny_chosen:
            return "Enter deny · ↑ back to approving"
        return "Type your vault passphrase · Enter approve · ↓ deny · PgUp/PgDn scroll"

    def handle(self, key: Key) -> None:
        if self.withdrawn:
            if key in ("enter", "escape", "ctrl_c"):
                self.app.dismiss_card()
        elif key in ("page_up", "page_down"):
            self.offset += PAGE if key == "page_down" else -PAGE
        elif key == "up":
            self.deny_chosen = False
        elif key in ("down", "escape", "ctrl_c"):
            self.deny_chosen = True
        elif key == "enter":
            self._submit()
        elif self.field.handle("\t" if key == "tab" else key):
            self.deny_chosen = False
            self.problem = None

    def _submit(self) -> None:
        if self.deny_chosen:
            self.app.deny(self)
        elif self.field.text:
            typed = self.field.text
            self.field.clear()
            self.app.approve(self, typed)
        else:
            self.problem = "Type your vault passphrase first, or choose Deny."

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        title, summary, details = content(self.request, self.app.broker)
        lines: list[Line] = [styled(summary, BOLD), []]
        for label, values in details:
            for number, value in enumerate(values):
                for part_number, part in enumerate(wrap_line(value, max(inner - LABEL_WIDTH, 10))):
                    shown = label if number == 0 and part_number == 0 else ""
                    lines.append([Span(f"{shown:<{LABEL_WIDTH}}", DIM), *part])
        footer = [[], *self._choices()]
        visible, self.offset, below = scrolled(lines, max(rows - 2 - len(footer), 3), self.offset)
        if below:
            visible = visible[:-1] + [styled(f"↓ {below + 1} more lines · PgDn to read them", YELLOW)]
        age = waited(self.app.broker.state.now() - self.request.created_at)
        return box(visible + footer, columns, styled(title, ACCENT), styled(f"waiting {age}", DIM), border=YELLOW)

    def _choices(self) -> list[Line]:
        if self.withdrawn:
            return [mark("warn", "Withdrawn: the program that asked went away."), styled("  What you type now is ignored. Press Enter to go on.", DIM)]
        lines = [mark("error", self.problem)] if self.problem else []
        approve = [Span(f"{POINTER} " if not self.deny_chosen else "  ", CYAN), Span("Approve  ", "" if self.deny_chosen else BOLD)]
        prompt = [Span(f"[{self.app.phrase}] ", CYAN), Span("Vault passphrase: "), *self.field.render(30)]
        deny = [Span(f"{POINTER} " if self.deny_chosen else "  ", CYAN), Span("Deny", BOLD if self.deny_chosen else "")]
        return [*lines, approve + prompt, deny]
