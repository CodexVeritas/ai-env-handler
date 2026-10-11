"""The home screen: what happened, newest at the bottom, and a command line for /commands with earlier commands on ↑.

Only / starts a command. Other typing is not shown and does nothing, so a passphrase typed when no request is on screen
never appears there."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from envh.server.console import keys, logs, presets, sessions, settings
from envh.server.console.activity import earlier_marker, transcript
from envh.server.console.dialogs import Choice, Info, View
from envh.server.console.editor import start_edit, start_passphrase_change
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, POINTER, Key, Line, Paste, Span, TextField, box, styled, text_width, wrap

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

SUGGESTIONS_SHOWN = 8


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    run: Callable[[ConsoleApp], None]


def confirm_quit(app: ConsoleApp) -> None:
    app.open(Choice(
        app,
        "Stop the console?",
        [("Keep it running", lambda: None), ("Stop the console", app.stop)],
        lines=[styled("Live sessions end, and nothing can be approved until you start it again.")],
    ))


def lock(app: ConsoleApp) -> None:
    if not app.unlocked:
        app.tell("Changes are already locked; the next one asks for your passphrase.")
        return
    app.lock("Changes locked; the next one asks for your passphrase")
    app.tell("Locked. The next change asks for your passphrase.", "ok")


def reload(app: ConsoleApp) -> None:
    if not app.attempt(app.broker.reload):
        return
    app.changed()
    app.tell(f"Read config.yaml and presets.yaml again: {len(app.broker.config.presets)} presets.", "ok")


COMMANDS = (
    Command("keys", "See, add, rename, describe and remove keys", lambda app: app.push(keys.KeysView(app))),
    Command("presets", "Create and edit presets", lambda app: app.push(presets.PresetsView(app))),
    Command("settings", "Approval rules, session limits, notifications, users", lambda app: app.push(settings.SettingsView(app))),
    Command("sessions", "Live sessions and running commands; end a session", lambda app: app.push(sessions.SessionsView(app))),
    Command("add", "Store a new key", keys.start_add),
    Command("passphrase", "Change the vault passphrase", start_passphrase_change),
    Command("lock", "Lock changes now; the next one asks for your passphrase", lock),
    Command("logs", "What happened, from earlier runs of the console too", lambda app: app.push(logs.LogsView(app))),
    Command("edit-config", "Open config.yaml in a text editor", lambda app: start_edit(app, "config")),
    Command("edit-presets", "Open presets.yaml in a text editor", lambda app: start_edit(app, "presets")),
    Command("reload", "Read config.yaml and presets.yaml again", reload),
    Command("help", "Keys and shortcuts", lambda app: app.open(help_dialog(app))),
    Command("quit", "Stop the console; live sessions end", confirm_quit),
)


def matching(text: str) -> list[Command]:
    """Commands whose name starts with what was typed, then those that contain it, then those whose summary does."""
    query = text.lstrip("/").lower()
    found = [command for command in COMMANDS if command.name.startswith(query)]
    found += [command for command in COMMANDS if query in command.name and command not in found]
    return found + [command for command in COMMANDS if query in command.summary.lower() and command not in found]


def command_character(char: str) -> str:
    return char.lower() if char.isascii() and (char.isalnum() or char in "-/") else ""


def help_dialog(app: ConsoleApp) -> Info:
    rows = [
        ("/", "commands; ↑ brings back earlier ones"),
        ("↑↓ PgUp PgDn", "move, scroll"),
        ("Enter", "open, choose, confirm"),
        ("F2", "rename the selected key or preset"),
        ("Del", "remove the selected item"),
        ("Ctrl-S", "save changes to presets or settings"),
        ("Esc", "back, cancel"),
        ("Ctrl-L", "redraw the screen"),
        ("Ctrl-C", "back; twice on this screen stops the console"),
    ]
    lines: list[Line] = [[Span(f"{key:<14}", CYAN), Span(text)] for key, text in rows]
    lines += [
        [],
        styled("Approving", BOLD),
        styled("A request takes over the screen when it arrives. Type your vault passphrase and press Enter to approve it, or press ↓ and Enter to deny it."),
        [Span("Type the passphrase only where your console phrase is shown: "), Span(app.phrase, CYAN)],
        styled("A change to keys, presets or settings asks for it too; for an hour after that, changes ask only to be confirmed. /lock ends the hour early. Approving a request and changing the passphrase always ask."),
        [],
        styled('Details: README, "The console"', DIM),
    ]
    return Info(app, "Shortcuts", lines)


class HomeView(View):
    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(app)
        self.field = TextField(accept=command_character)
        self.choice = 0
        self.recalled: int | None = None
        self.scroll = 0

    def typing(self) -> bool:
        return bool(self.field.text) and self.recalled is None

    def hints(self) -> str:
        if self.field.text:
            return "Enter run · ↑↓ choose · Tab complete · Esc clear" if self.recalled is None else "Enter run · ↑↓ earlier and later commands · Esc clear"
        return "/ commands · ↑ earlier commands · PgUp/PgDn activity · ? shortcuts · Ctrl-C twice quits"

    def handle(self, key: Key) -> None:
        if self.field.text:
            self._edit(key)
        elif key == "/":
            self.field.insert("/")
            self.choice = 0
        elif key == "up":
            self._recall(-1)
        elif key in ("page_up", "page_down", "home", "end"):
            self.scroll = {"page_up": self.scroll + 10, "page_down": self.scroll - 10, "home": 1_000_000, "end": 0}[key]
        elif key == "?":
            self.app.open(help_dialog(self.app))
        elif key == "ctrl_c":
            self.app.request_quit()
        elif isinstance(key, Paste) or (len(key) == 1 and key not in " "):
            self.app.tell("Commands start with /. What you typed was not shown.")

    def _edit(self, key: Key) -> None:
        if key in ("escape", "ctrl_c"):
            self._reset()
        elif key == "enter":
            self._run()
        elif key in ("up", "down") and self.recalled is not None:
            self._recall(-1 if key == "up" else 1)
        elif key in ("up", "down"):
            self.choice = max(min(self.choice + (1 if key == "down" else -1), len(matching(self.field.text)) - 1), 0)
        elif key == "tab":
            found = matching(self.field.text)
            if found:
                self.field = TextField("/" + found[min(self.choice, len(found) - 1)].name, accept=command_character)
                self.choice = 0
        elif self.field.handle(key):
            self.recalled = None
            self.choice = 0
            if not self.field.text:
                self._reset()

    def _recall(self, step: int) -> None:
        history = self.app.history
        position = (len(history) if self.recalled is None else self.recalled) + step
        if not history or position < 0:
            return
        if position >= len(history):
            self._reset()
            return
        self.recalled = position
        self.field = TextField(history[position], accept=command_character)

    def _reset(self) -> None:
        self.field.clear()
        self.recalled = None
        self.choice = 0

    def _run(self) -> None:
        text = self.field.text
        exact = next((command for command in COMMANDS if "/" + command.name == text), None)
        found = matching(text)
        command = exact or (found[min(self.choice, len(found) - 1)] if found and self.recalled is None else None)
        self._reset()
        if command is None:
            self.app.tell(f"No command {text}. Type / to see them all.", "warn")
            return
        self.app.run_command(command)

    def body(self, columns: int, rows: int) -> list[Line]:
        bottom = [] if self.app.overlay_open() else [*self._input_box(columns), *self._suggestions(columns)]
        room = max(rows - len(bottom), 0)
        lines = self._transcript(columns)
        if len(lines) <= room:
            return lines + [[] for _ in range(room - len(lines))] + bottom
        self.scroll = min(max(self.scroll, 0), len(lines) - room)
        end = len(lines) - self.scroll
        above = end - room
        visible = lines[above:end]
        if above:
            visible = [earlier_marker(above + 1), *visible[1:]]
        return visible + bottom

    def status(self) -> Line | None:
        return styled("↓ newer activity below · End shows the latest", DIM) if self.scroll else None

    def _transcript(self, columns: int) -> list[Line]:
        return welcome(columns) + transcript(self.app.activity.entries, columns)

    def _input_box(self, columns: int) -> list[Line]:
        if self.field.text:
            line = [Span("> ", BOLD), *self.field.render(columns - 6)]
        else:
            line = [Span("> ", BOLD), Span("Type / for commands", DIM)]
        return box([line], columns)

    def _suggestions(self, columns: int) -> list[Line]:
        if not self.field.text or self.recalled is not None:
            return []
        found = matching(self.field.text)
        if not found:
            return [styled("    No command matches. Esc clears.", DIM)]
        self.choice = min(self.choice, len(found) - 1)
        first = max(self.choice - SUGGESTIONS_SHOWN + 1, 0)
        lines = []
        for index, command in enumerate(found[first:first + SUGGESTIONS_SHOWN], start=first):
            chosen = index == self.choice
            lines.append([
                Span(f"  {POINTER} " if chosen else "    ", CYAN),
                Span(f"/{command.name:<14}", ACCENT if chosen else ""),
                Span(command.summary, "" if chosen else DIM),
            ])
        return lines


def welcome(columns: int) -> list[Line]:
    width = min(columns, 72)
    inner = width - 4
    lines: list[Line] = [styled("envh console", ACCENT)]
    lines += [styled(part) for part in wrap("Requests from your scripts and agents appear here on their own. Approve one with your vault passphrase.", inner)]
    lines.append([])
    for name, text in (("keys", "see, add, rename and describe keys"), ("presets", "create and edit presets"), ("settings", "approval rules, session limits, users")):
        lines.append([Span(f"/{name:<10}", CYAN), Span(text, DIM)])
    if text_width("Type / for every command, ? for shortcuts.") <= inner:
        lines += [[], styled("Type / for every command, ? for shortcuts.", DIM)]
    return box(lines, width)
