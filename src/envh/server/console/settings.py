"""The Settings screens: every option in config.yaml with its value and what it does, and the rules for single keys.
Edits stay in a draft until Ctrl-S, which shows every change and asks for the vault passphrase once."""

from __future__ import annotations

import pwd
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from envh.core.broker import RequestError
from envh.core.config import AUTO, CONFIG_FILE, ConfigError, SecretEntry, Settings, parse_config_for_broker, parse_settings, render_config
from envh.core.durations import DurationError, format_duration, parse_duration
from envh.server.console.dialogs import Choice, KeyPicker, Prompt, View, text_lines
from envh.server.console.presets import check_duration
from envh.server.console.summaries import merge, plural, rule_text, settings_changes, settings_fields, settings_from_fields
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, POINTER, YELLOW, Key, Line, Selection, Span, mark, styled, window

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

SECTIONS = {"notify": "When a request waits", "users": "Who can ask", "approval": "Defaults for every key", "rule": "Rules for single keys"}
LABELS = {"notify": "Notifications", "users": "Allowed users", "approval": "Approval", "max_session": "Longest session", "add": "+ Add a rule for a key…"}
EXPLANATIONS = {
    "notify": "A desktop notification with a chime, and the console's bell, when a request waits. Reminders follow after 30 s, 1, 2 and 5 minutes, then every 5.",
    "users": "Logins whose scripts and agents may ask this console for keys. Each one sees only their own sessions.",
    "approval": "session: one approval opens a session that many runs share. per-run: every run asks you.",
    "max_session": "The longest session that may include a key; a key's own rule or a preset can lower it. Up to 24h.",
    "rule": "This key's own rule; what it doesn't set follows the defaults.",
    "add": "Give one key its own approval or session limit, such as per-run for a production database, or auto for a key that needs no approval.",
}
AUTO_TEXT = "auto: runs get it without asking you"
LOGIN_SEPARATORS = re.compile(r"[\s,]+")


def open_rule(app: ConsoleApp, name: str) -> None:
    """Settings, with the rule for one key open."""
    view = SettingsView(app)
    app.push(view)
    app.push(RuleView(app, view, name))


def login_character(char: str) -> str:
    return char if char.isascii() and (char.isalnum() or char in "._-, ") else ""


class SettingsView(View):
    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(app)
        self.base = app.broker.config.settings
        self.draft = self.base
        self.selection = Selection()

    def title(self) -> str:
        return "Settings"

    def hints(self) -> str:
        return "↑↓ move · Enter change · Ctrl-S save · Esc back"

    def rules(self) -> list[str]:
        return sorted(name for name, entry in self.draft.secrets.items() if rule_text(entry))

    def rows(self) -> list[tuple[str, str]]:
        return [("notify", ""), ("users", ""), ("approval", ""), ("max_session", ""), *(("rule", name) for name in self.rules()), ("add", "")]

    def status(self) -> Line | None:
        changed = settings_changes(self.base, self.draft)
        return mark("warn", f"{plural(len(changed), 'change')} not saved yet · Ctrl-S saves · Esc reviews", YELLOW) if changed else None

    def set_entry(self, name: str, entry: SecretEntry) -> None:
        secrets = {other: value for other, value in self.draft.secrets.items() if other != name}
        if not entry.empty:
            secrets[name] = entry
        self.draft = replace(self.draft, secrets=secrets)

    def handle(self, key: Key) -> None:
        rows = self.rows()
        if self.selection.move(key, len(rows)):
            return
        kind, name = rows[self.selection.index]
        if key == "enter":
            self.change(kind, name)
        elif key == "delete" and kind == "rule":
            self.set_entry(name, replace(self.draft.secrets[name], approval=None, max_session=None))
            self.app.tell(f"Removed the rule for {name}; it follows the defaults once you save.")
        elif key == "ctrl_s":
            self.save()
        elif key in ("escape", "ctrl_c"):
            self.leave()

    def change(self, kind: str, name: str) -> None:
        if kind == "notify":
            self.draft = replace(self.draft, notify=not self.draft.notify)
        elif kind == "users":
            hint = "Their login names, separated by commas."
            self.app.open(Prompt(self.app, "Allowed users", self._set_users, initial=", ".join(self.draft.users), accept=login_character, hint=hint))
        elif kind == "approval":
            options = [
                ("session: one approval opens a session", lambda: self._set_approval("session")),
                ("per-run: every run asks you", lambda: self._set_approval("per-run")),
            ]
            self.app.open(Choice(self.app, "Approval for every key", options, selected=0 if self.draft.defaults.approval == "session" else 1))
        elif kind == "max_session":
            hint = "Like 30m or 2h, up to 24h."
            self.app.open(Prompt(self.app, "Longest session", self._set_max_session, initial=format_duration(self.draft.defaults.max_session), hint=hint))
        elif kind == "rule":
            self.app.push(RuleView(self.app, self, name))
        else:
            names = [key_name for key_name in self.app.broker.vault.names() if key_name not in self.rules()]
            self.app.open(KeyPicker(self.app, "Which key gets its own rule?", names, lambda picked: self.app.push(RuleView(self.app, self, picked))))

    def _set_users(self, text: str) -> None:
        users = tuple(dict.fromkeys(name for name in LOGIN_SEPARATORS.split(text) if name))
        unknown = [name for name in users if not login_exists(name)]
        if not users:
            self.app.tell("At least one user is needed, or nobody could ask for keys. Nothing changed.", "warn")
        elif unknown:
            self.app.tell(f"No login named {', '.join(unknown)} on this computer. Nothing changed.", "warn")
        else:
            self.draft = replace(self.draft, users=users)

    def _set_approval(self, approval: str) -> None:
        self.draft = replace(self.draft, defaults=replace(self.draft.defaults, approval=approval))

    def _set_max_session(self, text: str) -> None:
        try:
            self.draft = replace(self.draft, defaults=replace(self.draft.defaults, max_session=parse_duration(check_duration(text.strip()))))
        except DurationError as error:
            self.app.tell(f"{error}. Nothing changed.", "warn")

    def changes(self) -> list[Line]:
        lines = settings_changes(self.base, self.draft)
        try:
            on_disk = (self.app.broker.data_dir / CONFIG_FILE).read_text()
            hand_written = on_disk != render_config(parse_settings(on_disk))
        except (OSError, ConfigError):
            hand_written = False
        if hand_written:
            lines += [[], styled("Saving rewrites config.yaml in envh's layout; comments added there by hand are not kept.", DIM)]
        return lines

    def save(self, then_leave: bool = False) -> None:
        if not settings_changes(self.base, self.draft):
            self.app.tell("Nothing to save.")
            return
        try:
            parse_config_for_broker(render_config(self.draft))
        except ConfigError as error:
            self.app.tell(f"Can't save yet: {error}", "warn")
            return
        self.app.ask_passphrase("Save settings", self.changes(), lambda: self._apply(then_leave))

    def _apply(self, then_leave: bool) -> None:
        current = self.app.broker.config.settings
        merged, conflicts = merge(settings_fields(self.base), settings_fields(self.draft), settings_fields(current))
        if conflicts:
            raise RequestError(f"{', '.join(conflicts)} changed while you were editing; nothing saved. Leave without saving, then edit again.")
        self.app.broker.save_settings(settings_from_fields(merged))
        self.base = self.draft = self.app.broker.config.settings
        self.app.changed()
        self.app.tell("Saved the settings.", "ok")
        if then_leave:
            self.app.pop(self)

    def leave(self) -> None:
        changes = settings_changes(self.base, self.draft)
        if not changes:
            self.app.pop(self)
            return
        options = [("Save them…", lambda: self.save(then_leave=True)), ("Throw them away", lambda: self.app.pop(self)), ("Keep editing", lambda: None)]
        self.app.open(Choice(self.app, f"{plural(len(changes), 'change')} not saved", options, lines=changes))

    def body(self, columns: int, rows: int) -> list[Line]:
        lines: list[Line] = []
        focus = 0
        label_width = max(max((len(name) for name in self.rules()), default=0) + 2, 20)
        all_rows = self.rows()
        first_rule = next(index for index, (kind, _) in enumerate(all_rows) if kind in ("rule", "add"))
        for index, (kind, name) in enumerate(all_rows):
            section = SECTIONS["rule"] if index == first_rule else SECTIONS.get(kind) if kind != "rule" else None
            if section:
                lines += [*([[]] if lines else []), styled(f"  {section}", BOLD)]
            chosen = index == self.selection.index
            if chosen:
                focus = len(lines)
            pointer = Span(f"  {POINTER} " if chosen else "    ", CYAN)
            if kind == "add":
                lines.append([pointer, Span(LABELS["add"], ACCENT if chosen else CYAN)])
                continue
            shown = setting_value(self.draft, kind, name)
            changed = shown != setting_value(self.base, kind, name)
            lines.append([pointer, Span(f"{LABELS.get(kind, name):<{label_width}}", BOLD if chosen else ""), Span(shown), Span("  changed" if changed else "", YELLOW)])
        explanation = [[Span("    "), *line] for line in text_lines(EXPLANATIONS[all_rows[self.selection.index][0]], max(columns - 8, 20), DIM)]
        return window(lines, focus, max(rows - len(explanation) - 1, 1)) + [[]] + explanation


def setting_value(settings: Settings, kind: str, name: str) -> str:
    if kind == "notify":
        return "on" if settings.notify else "off"
    if kind == "users":
        return ", ".join(settings.users)
    if kind == "approval":
        return settings.defaults.approval
    if kind == "max_session":
        return format_duration(settings.defaults.max_session)
    return rule_text(settings.secrets.get(name, SecretEntry())) or "follows the defaults"


def login_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


class RuleView(View):
    """One key's own rule: its approval and its longest session, each either set here or following the defaults."""

    ACTIONS = ("approval", "max_session", "remove")

    def __init__(self, app: ConsoleApp, parent: SettingsView, name: str) -> None:
        super().__init__(app)
        self.parent = parent
        self.name = name
        self.selection = Selection()

    @property
    def entry(self) -> SecretEntry:
        return self.parent.draft.secrets.get(self.name, SecretEntry())

    def title(self) -> str:
        return f"Rule for {self.name}"

    def hints(self) -> str:
        return "↑↓ move · Enter change · Ctrl-S save · Esc back"

    def status(self) -> Line | None:
        return self.parent.status()

    def handle(self, key: Key) -> None:
        if self.selection.move(key, len(self.ACTIONS)):
            return
        action = self.ACTIONS[self.selection.index]
        defaults = self.parent.draft.defaults
        if key == "enter" and action == "approval":
            options = [
                (f"Follow the default: {defaults.approval}", lambda: self._set(approval=None)),
                ("session: one approval opens a session", lambda: self._set(approval="session")),
                ("per-run: every run asks you", lambda: self._set(approval="per-run")),
                (AUTO_TEXT, lambda: self._set(approval=AUTO)),
            ]
            self.app.open(Choice(self.app, f"Approval for {self.name}", options, selected={None: 0, "session": 1, "per-run": 2, AUTO: 3}[self.entry.approval]))
        elif key == "enter" and action == "max_session":
            hint = f"Like 30m or 8h, up to 24h. Empty follows the default ({format_duration(defaults.max_session)})."
            initial = format_duration(self.entry.max_session) if self.entry.max_session else ""
            self.app.open(Prompt(self.app, f"Longest session for {self.name}", self._set_max_session, initial=initial, hint=hint))
        elif key == "enter":
            self._set(approval=None, max_session=None)
            self.app.pop(self)
            self.app.tell(f"Removed the rule for {self.name}; it follows the defaults once you save.")
        elif key == "ctrl_s":
            self.parent.save()
        else:
            super().handle(key)

    def _set(self, **fields: object) -> None:
        self.parent.set_entry(self.name, replace(self.entry, **fields))

    def _set_max_session(self, text: str) -> None:
        if not text.strip():
            self._set(max_session=None)
            return
        try:
            self._set(max_session=parse_duration(check_duration(text.strip())))
        except DurationError as error:
            self.app.tell(f"{error}. Nothing changed.", "warn")

    def body(self, columns: int, rows: int) -> list[Line]:
        defaults = self.parent.draft.defaults
        entry = self.entry
        values = {
            "approval": entry.approval or f"follows the default ({defaults.approval})",
            "max_session": format_duration(entry.max_session) if entry.max_session else f"follows the default ({format_duration(defaults.max_session)})",
            "remove": "",
        }
        labels = {"approval": "Approval", "max_session": "Longest session", "remove": "Remove this rule"}
        lines: list[Line] = [styled(f"  How {self.name} is approved, instead of the defaults.", DIM), []]
        for index, action in enumerate(self.ACTIONS):
            chosen = index == self.selection.index
            set_here = (action == "approval" and entry.approval) or (action == "max_session" and entry.max_session)
            lines.append([Span(f"  {POINTER} " if chosen else "    ", CYAN), Span(f"{labels[action]:<18}", BOLD if chosen else ""), Span(values[action], "" if set_here else DIM)])
        return lines
