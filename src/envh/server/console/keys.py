"""The Keys screens: every key with the start and end of its value, where it is used and how it is approved, and the
changes to one key. Each change asks for the vault passphrase and is saved at once."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from envh.common import SECRET_NAME, peek
from envh.core.config import DESCRIPTION_LIMIT, SecretEntry
from envh.core.durations import format_duration
from envh.server.console.dialogs import Prompt, View, text_lines
from envh.server.console.settings import open_rule
from envh.server.console.summaries import approval_text, plural, rule_text
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, POINTER, Key, Line, Selection, Span, styled, typed_character
from envh.server.control import HandledErrors

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

ADD_ROW = "+ Add a key…"


def name_character(char: str) -> str:
    """What a typed character becomes in a key name: names are UPPER_CASE, and - . or a space become _."""
    if char in "-. ":
        return "_"
    return char.upper() if char.isascii() and (char.isalnum() or char == "_") else ""


def description_character(char: str) -> str:
    return char if typed_character(char) and char not in "\r\n\t" else ""


def start_add(app: ConsoleApp) -> None:
    app.open(Prompt(app, "Add a key", lambda name: _add_named(app, name), accept=name_character, hint="Its name, like OPENAI_API_KEY. Scripts ask for it by this name."))


def _add_named(app: ConsoleApp, name: str) -> None:
    if not SECRET_NAME.match(name):
        app.tell("A key name starts with a letter and has only letters, digits and _. Nothing added.", "warn")
    elif name in app.broker.vault:
        app.tell(f"{name} is already stored. Open it in /keys to replace its value.", "warn")
    else:
        app.open(Prompt(app, f"Value for {name}", lambda value: _confirm_store(app, name, value), hidden=True, hint="Paste or type it. It stays hidden."))


def start_replace(app: ConsoleApp, name: str) -> None:
    app.open(Prompt(app, f"New value for {name}", lambda value: _confirm_store(app, name, value), hidden=True, hint="Paste or type it. It stays hidden. Live sessions use it from their next run."))


def _confirm_store(app: ConsoleApp, name: str, value: str) -> None:
    if not value:
        app.tell("The value was empty. Nothing stored.")
        return
    details = [[Span("New value  ", DIM), Span(value_text(value))]]
    if name in app.broker.vault:
        details.insert(0, [Span("Now        ", DIM), Span(value_text(app.broker.vault.get(name)))])
    verb = "Replace the value of" if name in app.broker.vault else "Store"
    app.ask_passphrase(f"{verb} {name}", details, lambda: _store(app, name, value))


def _store(app: ConsoleApp, name: str, value: str) -> None:
    replacing = name in app.broker.vault
    app.broker.store_secret(name, value)
    app.changed()
    app.tell(f"Replaced the value of {name}." if replacing else f"Stored {name}.", "ok")


def value_text(value: str) -> str:
    """A value as the console shows it: its start and end, its length, and a short id that tells equal values apart."""
    return f"{peek(value)}  {plural(len(value), 'character')}  id {hashlib.sha256(value.encode()).hexdigest()[:6]}"


def start_rename(app: ConsoleApp, name: str) -> None:
    app.open(Prompt(app, f"Rename {name}", lambda new: _confirm_rename(app, name, new), initial=name, accept=name_character, hint="Type the new name. - . and spaces become _."))


def _confirm_rename(app: ConsoleApp, old: str, new: str) -> None:
    if new in ("", old):
        app.tell("Nothing renamed.")
        return
    try:
        app.broker.check_rename(old, new)
    except (*HandledErrors, OSError) as error:
        app.tell(f"Nothing renamed: {error}", "warn")
        return
    details = [
        styled("Presets, its settings and live sessions follow the new name."),
        styled(f"Scripts and .env comments that name {old} keep the old name.", DIM),
    ]
    app.ask_passphrase(f"Rename {old} to {new}", details, lambda: _rename(app, old, new))


def _rename(app: ConsoleApp, old: str, new: str) -> None:
    try:
        app.broker.rename_secret(old, new)
    except (*HandledErrors, OSError) as error:
        if new not in app.broker.vault:
            raise
        app.changed({old: new})
        app.tell(f"Renamed to {new}, but: {error}", "warn")
        return
    app.changed({old: new})
    app.tell(f"Renamed {old} to {new}.", "ok")


def start_remove(app: ConsoleApp, name: str) -> None:
    users = app.broker.config.presets_using(name)
    if users:
        app.tell(f"{name} is used by {'preset' if len(users) == 1 else 'presets'} {', '.join(users)}. Take it out of them in /presets first.", "warn")
        return
    details = [styled("Its value is deleted from the vault. This can't be undone.")]
    if name in app.broker.config.settings.secrets:
        details.append(styled("Its approval rule and description go too."))
    if name in app.broker.state.names_in_use():
        details.append(styled("A live session or waiting request uses it; runs that need it will fail.", DIM))
    app.ask_passphrase(f"Remove {name}", details, lambda: _remove(app, name))


def _remove(app: ConsoleApp, name: str) -> None:
    app.broker.remove_secret(name)
    app.changed()
    app.tell(f"Removed {name}.", "ok")


def start_describe(app: ConsoleApp, name: str) -> None:
    current = app.broker.config.description_for(name) or ""
    hint = "A few words on what it is for. envh list shows it to your scripts and agents, so never paste the key itself. Empty removes it."
    app.open(Prompt(app, f"Describe {name}", lambda text: _confirm_describe(app, name, text.strip()), initial=current, accept=description_character, hint=hint, limit=DESCRIPTION_LIMIT))


def _confirm_describe(app: ConsoleApp, name: str, text: str) -> None:
    if text == (app.broker.config.description_for(name) or ""):
        app.tell("Nothing changed.")
        return
    details = [styled(f'"{text}"')] if text else [styled("Removes the description.")]
    app.ask_passphrase(f"Describe {name}", details, lambda: _describe(app, name, text or None))


def _describe(app: ConsoleApp, name: str, text: str | None) -> None:
    settings = app.broker.config.settings
    entry = replace(settings.secrets.get(name, SecretEntry()), description=text)
    secrets = {other: value for other, value in settings.secrets.items() if other != name}
    if not entry.empty:
        secrets[name] = entry
    app.broker.save_settings(replace(settings, secrets=secrets))
    app.changed()
    app.tell(f"Saved the description of {name}." if text else f"Removed the description of {name}.", "ok")


class KeysView(View):
    def __init__(self, app: ConsoleApp, select: str | None = None) -> None:
        super().__init__(app)
        self.selection = Selection()
        self.names: list[str] = []
        self.refresh({}, select)

    def title(self) -> str:
        return "Keys"

    def hints(self) -> str:
        return "↑↓ move · Enter open · F2 rename · Del remove · Esc back"

    def refresh(self, renamed: dict[str, str], select: str | None = None) -> None:
        current = self.names[self.selection.index] if self.selection.index < len(self.names) else None
        self.names = self.app.broker.vault.names()
        wanted = select or renamed.get(current or "", current)
        if wanted in self.names:
            self.selection.index = self.names.index(wanted)

    def selected(self) -> str | None:
        return self.names[self.selection.index] if self.selection.index < len(self.names) else None

    def handle(self, key: Key) -> None:
        if self.selection.move(key, len(self.names) + 1):
            return
        name = self.selected()
        if key == "enter":
            if name is None:
                start_add(self.app)
            else:
                self.app.push(KeyDetailView(self.app, name))
        elif key == "f2" and name is not None:
            start_rename(self.app, name)
        elif key == "delete" and name is not None:
            start_remove(self.app, name)
        else:
            super().handle(key)

    def body(self, columns: int, rows: int) -> list[Line]:
        config = self.app.broker.config
        name_width = min(max((len(name) for name in self.names), default=4) + 2, max(columns * 2 // 5, 16))
        used_width = min(max((len(", ".join(config.presets_using(name))) for name in self.names), default=7) + 2, 24)
        header = f"    {'Name':<{name_width}}{'Value':<11}{'Used by':<{used_width}}{'Approval':<15}Description"
        intro = f"{plural(len(self.names), 'key')}. Values show only their first and last few characters, fewer for short ones."
        lines: list[Line] = [*text_lines(intro, columns - 2, DIM), [], styled(header, DIM)]
        visible = self.selection.visible(len(self.names) + 1, max(rows - len(lines), 1))
        for index in visible:
            chosen = index == self.selection.index
            pointer = Span(f"  {POINTER} " if chosen else "    ", CYAN)
            if index == len(self.names):
                lines.append([pointer, Span(ADD_ROW, ACCENT if chosen else CYAN)])
                continue
            name = self.names[index]
            lines.append([
                pointer,
                Span(f"{name:<{name_width}}", BOLD if chosen else ""),
                Span(f"{peek(self.app.broker.vault.get(name)):<11}", DIM),
                Span(f"{', '.join(config.presets_using(name)) or '-':<{used_width}}", "" if chosen else DIM),
                Span(f"{approval_text(config, name):<15}", "" if chosen else DIM),
                Span(config.description_for(name) or "", DIM),
            ])
        if not self.names:
            lines += [[], styled("    No keys yet. Add one here, or import a project's .env with: envh import <folder>", DIM)]
        return lines


class KeyDetailView(View):
    ACTIONS = ("Rename…", "Describe…", "Replace the value…", "Approval rule…", "Remove…")

    def __init__(self, app: ConsoleApp, name: str) -> None:
        super().__init__(app)
        self.name = name
        self.selection = Selection()

    def title(self) -> str:
        return self.name

    def hints(self) -> str:
        return "↑↓ move · Enter choose · Esc back"

    def refresh(self, renamed: dict[str, str]) -> None:
        self.name = renamed.get(self.name, self.name)
        if self.name not in self.app.broker.vault:
            self.app.pop(self)

    def handle(self, key: Key) -> None:
        if self.selection.move(key, len(self.ACTIONS)):
            return
        if key == "f2":
            start_rename(self.app, self.name)
        elif key == "delete":
            start_remove(self.app, self.name)
        elif key == "enter":
            action = self.ACTIONS[self.selection.index]
            if action == "Rename…":
                start_rename(self.app, self.name)
            elif action == "Describe…":
                start_describe(self.app, self.name)
            elif action == "Replace the value…":
                start_replace(self.app, self.name)
            elif action == "Approval rule…":
                open_rule(self.app, self.name)
            else:
                start_remove(self.app, self.name)
        else:
            super().handle(key)

    def body(self, columns: int, rows: int) -> list[Line]:
        broker = self.app.broker
        config = broker.config
        entry = config.settings.secrets.get(self.name, SecretEntry())
        policy = config.policy_for(self.name)
        source = "its own rule" if rule_text(entry) else "the defaults"
        approval = "per-run: every run asks you" if policy.approval == "per-run" else f"session, up to {format_duration(policy.max_session)}"
        rows_shown = [
            ("Value", value_text(broker.vault.get(self.name))),
            ("Description", config.description_for(self.name) or "none"),
            ("Used by", ", ".join(config.presets_using(self.name)) or "no preset"),
            ("Approval", f"{approval}  (from {source})"),
        ]
        lines: list[Line] = []
        for label, text in rows_shown:
            parts = text_lines(text, max(columns - 20, 10))
            lines.append([Span(f"  {label:<14}", DIM), *parts[0]])
            lines += [[Span(" " * 16), *part] for part in parts[1:]]
        lines.append([])
        for index, action in enumerate(self.ACTIONS):
            chosen = index == self.selection.index
            lines.append([Span(f"  {POINTER} " if chosen else "    ", CYAN), Span(action, BOLD if chosen else "")])
        return lines
