"""The Presets screens: a list of presets and an editor for one. Edits stay in a draft until Ctrl-S, which shows every
change and asks for the vault passphrase once; leaving with unsaved edits asks what to do with them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from envh.common import PRESET_NAME
from envh.core.broker import RequestError
from envh.core.config import VAR_NAME, ConfigError, dump_presets, parse_presets
from envh.core.durations import MAX_SESSION, DurationError, format_duration, parse_duration
from envh.server.console.dialogs import Choice, KeyPicker, Prompt, View, text_lines
from envh.server.console.summaries import PresetDraft, VarDraft, approval_text, merge, plural, preset_changes, preset_drafts, presets_raw
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, POINTER, RED, YELLOW, Key, Line, Selection, Span, mark, styled, window

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

NEW_ROW = "+ New preset…"
ADD_VARIABLE_ROW = "+ Add a variable…"


def preset_character(char: str) -> str:
    if char in " _":
        return "-"
    return char.lower() if char.isascii() and (char.isalnum() or char in ".-") else ""


def variable_character(char: str) -> str:
    if char in "-. ":
        return "_"
    return char if char.isascii() and (char.isalnum() or char == "_") else ""


def check_duration(text: str) -> str:
    """text as a session limit, like 30m or 3h; raises DurationError when it is not one."""
    duration = parse_duration(text)
    if duration > MAX_SESSION:
        raise DurationError(f"{text} is longer than the {format_duration(MAX_SESSION)} cap")
    return format_duration(duration)


class PresetsView(View):
    """The draft of every preset. renamed maps a preset's new name to the one it was saved under."""

    def __init__(self, app: ConsoleApp) -> None:
        super().__init__(app)
        self.base = preset_drafts(app.broker.config.presets_raw)
        self.draft = dict(self.base)
        self.renamed: dict[str, str] = {}
        self.selection = Selection()

    def title(self) -> str:
        return "Presets"

    def hints(self) -> str:
        return "↑↓ move · Enter open · F2 rename · Del remove · Ctrl-S save · Esc back"

    def removed(self) -> list[str]:
        return sorted(name for name in self.base if name not in self.draft and name not in self.renamed.values())

    def rows(self) -> list[tuple[str, str]]:
        return [*(("preset", name) for name in sorted(self.draft)), *(("removed", name) for name in self.removed()), ("new", "")]

    def unsaved(self) -> list[str]:
        return sorted(name for name in set(self.base) | set(self.draft) if self.base.get(name) != self.draft.get(name))

    def status(self) -> Line | None:
        changed = self.unsaved()
        return mark("warn", f"{plural(len(changed), 'preset')} changed and not saved yet · Ctrl-S saves · Esc reviews", YELLOW) if changed else None

    def changes(self) -> list[Line]:
        return preset_changes(self.base, self.draft, self.renamed)

    def handle(self, key: Key) -> None:
        rows = self.rows()
        if self.selection.move(key, len(rows)):
            return
        kind, name = rows[self.selection.index]
        if key == "enter":
            if kind == "preset":
                self.app.push(PresetEditorView(self.app, self, name))
            elif kind == "removed":
                self.restore(name)
            else:
                self.start_new()
        elif key == "f2" and kind == "preset":
            self.start_rename(name)
        elif key == "delete" and kind == "preset":
            self.remove(name)
        elif key == "delete" and kind == "removed":
            self.restore(name)
        elif key == "ctrl_s":
            self.save()
        elif key in ("escape", "ctrl_c"):
            self.leave()

    def remove(self, name: str) -> None:
        del self.draft[name]
        original = self.renamed.pop(name, None)
        if original is not None and original not in self.draft:
            self.app.tell(f"Removed {name} (saved as {original}). Del on {original} brings it back.")
        else:
            self.app.tell(f"Removed {name}. Del brings it back until you save.")

    def restore(self, name: str) -> None:
        self.draft[name] = self.base[name]
        self.app.tell(f"Brought back {name}.")

    def start_new(self) -> None:
        hint = "Lowercase letters, digits, - and ., like news-bot or forecasting.team. Scripts ask for it with --preset NAME."
        self.app.open(Prompt(self.app, "New preset", self._create, accept=preset_character, hint=hint))

    def _create(self, name: str) -> None:
        if not PRESET_NAME.match(name):
            self.app.tell("A preset name starts with a letter or digit and has only lowercase letters, digits, - and .", "warn")
        elif name in self.draft:
            self.app.tell(f"There is already a preset named {name}.", "warn")
        else:
            self.draft[name] = PresetDraft(env={})
            editor = PresetEditorView(self.app, self, name)
            self.app.push(editor)
            editor.start_add_variable()

    def start_rename(self, name: str) -> None:
        self.app.open(Prompt(self.app, f"Rename preset {name}", lambda new: self._rename(name, new), initial=name, accept=preset_character))

    def _rename(self, old: str, new: str) -> None:
        if new in ("", old):
            self.app.tell("Nothing renamed.")
        elif not PRESET_NAME.match(new):
            self.app.tell("A preset name starts with a letter or digit and has only lowercase letters, digits, - and .", "warn")
        elif new in self.draft:
            self.app.tell(f"There is already a preset named {new}.", "warn")
        else:
            self.draft[new] = self.draft.pop(old)
            original = self.renamed.pop(old, old)
            if original != new and original in self.base:
                self.renamed[new] = original
            self.selection.index = self.rows().index(("preset", new))
            self.app.tell(f"Renamed {old} to {new}. Scripts that ask for {original} need the new name once you save.")

    def save(self, then_leave: bool = False) -> None:
        if not self.unsaved():
            self.app.tell("Nothing to save.")
            return
        try:
            parse_presets(dump_presets(presets_raw(self.draft)), set(self.app.broker.vault.names()))
        except ConfigError as error:
            self.app.tell(f"Can't save yet: {error}", "warn")
            return
        self.app.ask_passphrase("Save preset changes", self.changes(), lambda: self._apply(then_leave))

    def _apply(self, then_leave: bool) -> None:
        current = preset_drafts(self.app.broker.config.presets_raw)
        merged, conflicts = merge(self.base, self.draft, current)
        if conflicts:
            raise RequestError(f"{', '.join(conflicts)} changed while you were editing; nothing saved. Leave without saving, then edit again.")
        self.app.broker.save_presets(presets_raw(merged))
        self.base = preset_drafts(self.app.broker.config.presets_raw)
        self.draft = dict(self.base)
        self.renamed = {}
        self.app.changed()
        self.app.tell("Saved the presets.", "ok")
        if then_leave:
            self.app.pop(self)

    def leave(self) -> None:
        if not self.unsaved():
            self.app.pop(self)
            return
        options = [("Save them…", lambda: self.save(then_leave=True)), ("Throw them away", lambda: self.app.pop(self)), ("Keep editing", lambda: None)]
        self.app.open(Choice(self.app, f"{plural(len(self.unsaved()), 'preset')} changed", options, lines=self.changes()))

    def body(self, columns: int, rows: int) -> list[Line]:
        intro = "A preset names the variables a script gets and the key each one reads. Scripts ask for one with --preset NAME."
        lines: list[Line] = [*text_lines(intro, columns - 2, DIM), []]
        names = [name for kind, name in self.rows() if kind != "new"]
        name_width = min(max((len(name) for name in names), default=4) + 2, max(columns // 3, 12))
        lines.append(styled(f"    {'Name':<{name_width}}{'Longest session':<17}Variables", DIM))
        all_rows = self.rows()
        for index in self.selection.visible(len(all_rows), max(rows - len(lines), 1)):
            kind, name = all_rows[index]
            chosen = index == self.selection.index
            pointer = Span(f"  {POINTER} " if chosen else "    ", CYAN)
            if kind == "new":
                lines.append([pointer, Span(NEW_ROW, ACCENT if chosen else CYAN)])
            elif kind == "removed":
                lines.append([pointer, Span(f"{name:<{name_width}}", RED), Span("removed · Del brings it back", DIM)])
            else:
                preset = self.draft[name]
                tag = " (new)" if name not in self.base and name not in self.renamed else " (renamed)" if name in self.renamed else " (changed)" if preset != self.base.get(name) else ""
                lines.append([
                    pointer,
                    Span(f"{name:<{name_width}}", BOLD if chosen else ""),
                    Span(f"{preset.max_session or '-':<17}", DIM),
                    Span(", ".join(sorted(preset.env)) or "no variables yet", "" if preset.env else RED),
                    Span(tag, YELLOW),
                ])
        return lines


class PresetEditorView(View):
    def __init__(self, app: ConsoleApp, parent: PresetsView, name: str) -> None:
        super().__init__(app)
        self.parent = parent
        self.name = name
        self.selection = Selection()

    @property
    def preset(self) -> PresetDraft:
        return self.parent.draft[self.name]

    def update(self, preset: PresetDraft) -> None:
        self.parent.draft[self.name] = preset

    def title(self) -> str:
        return self.name

    def hints(self) -> str:
        return "↑↓ move · Enter change · F2 rename variable · Del remove variable · Ctrl-S save · Esc back"

    def status(self) -> Line | None:
        return self.parent.status()

    def rows(self) -> list[tuple[str, str]]:
        return [("limit", ""), *(("var", var) for var in sorted(self.preset.env)), ("add", "")]

    def handle(self, key: Key) -> None:
        rows = self.rows()
        if self.selection.move(key, len(rows)):
            return
        kind, var = rows[self.selection.index]
        if key == "enter":
            if kind == "limit":
                self.start_limit()
            elif kind == "var":
                self.variable_menu(var)
            else:
                self.start_add_variable()
        elif key == "f2" and kind == "var":
            self.start_rename_variable(var)
        elif key == "delete" and kind == "var":
            self.remove_variable(var)
        elif key == "ctrl_s":
            self.parent.save()
        elif key in ("escape", "ctrl_c"):
            self.app.pop(self)

    def start_limit(self) -> None:
        hint = "Like 30m or 3h, up to 24h. Empty: no limit of its own; each key's limit still applies."
        self.app.open(Prompt(self.app, f"Longest session for {self.name}", self._set_limit, initial=self.preset.max_session or "", hint=hint))

    def _set_limit(self, text: str) -> None:
        if not text.strip():
            self.update(replace(self.preset, max_session=None))
            return
        try:
            self.update(replace(self.preset, max_session=check_duration(text.strip())))
        except DurationError as error:
            self.app.tell(f"{error}. Nothing changed.", "warn")

    def variable_menu(self, var: str) -> None:
        entry = self.preset.env[var]
        approval = f"{entry.approval} (set here)" if entry.approval else f"{approval_text(self.app.broker.config, entry.secret)} (from the key)"
        options = [
            ("Read a different key…", lambda: self.start_pick_key(var)),
            (f"Approval: {approval}…", lambda: self.approval_menu(var)),
            ("Rename the variable…", lambda: self.start_rename_variable(var)),
            ("Remove the variable", lambda: self.remove_variable(var)),
        ]
        self.app.open(Choice(self.app, entry.text(var), options))

    def approval_menu(self, var: str) -> None:
        entry = self.preset.env[var]
        key_approval = approval_text(self.app.broker.config, entry.secret)
        options = [
            (f"Follow the key: {key_approval}", lambda: self.set_variable(var, replace(entry, approval=None))),
            ("session: one approval opens a session", lambda: self.set_variable(var, replace(entry, approval="session"))),
            ("per-run: every run asks you", lambda: self.set_variable(var, replace(entry, approval="per-run"))),
        ]
        selected = {None: 0, "session": 1, "per-run": 2}[entry.approval]
        self.app.open(Choice(self.app, f"Approval for {var} in {self.name}", options, selected=selected))

    def start_add_variable(self) -> None:
        hint = "The environment variable the script reads, like OPENAI_API_KEY. Next you pick the key it gets."
        self.app.open(Prompt(self.app, f"Add a variable to {self.name}", self._add_variable, accept=variable_character, hint=hint))

    def _add_variable(self, var: str) -> None:
        if not VAR_NAME.match(var):
            self.app.tell("A variable name starts with a letter or _ and has only letters, digits and _.", "warn")
        elif var in self.preset.env:
            self.app.tell(f"{self.name} already has {var}. Open it to change its key.", "warn")
        else:
            self.start_pick_key(var)

    def start_pick_key(self, var: str) -> None:
        names = self.app.broker.vault.names()
        current = self.preset.env.get(var)
        guess = current.secret if current else next((name for name in names if name == var or name.endswith("_" + var)), None)
        self.app.open(KeyPicker(self.app, f"Which key does {var} read?", names, lambda secret: self.set_variable(var, VarDraft(secret, current.approval if current else None)), guess))

    def set_variable(self, var: str, entry: VarDraft) -> None:
        self.update(replace(self.preset, env={**self.preset.env, var: entry}))
        self.selection.index = self.rows().index(("var", var))

    def start_rename_variable(self, var: str) -> None:
        self.app.open(Prompt(self.app, f"Rename {var}", lambda new: self._rename_variable(var, new), initial=var, accept=variable_character))

    def _rename_variable(self, old: str, new: str) -> None:
        if new in ("", old):
            self.app.tell("Nothing renamed.")
        elif not VAR_NAME.match(new):
            self.app.tell("A variable name starts with a letter or _ and has only letters, digits and _.", "warn")
        elif new in self.preset.env:
            self.app.tell(f"{self.name} already has {new}.", "warn")
        else:
            env = {(new if var == old else var): entry for var, entry in self.preset.env.items()}
            self.update(replace(self.preset, env=env))
            self.selection.index = self.rows().index(("var", new))

    def remove_variable(self, var: str) -> None:
        self.update(replace(self.preset, env={name: entry for name, entry in self.preset.env.items() if name != var}))
        self.app.tell(f"Removed {var} from {self.name}.")

    def body(self, columns: int, rows: int) -> list[Line]:
        preset = self.preset
        config = self.app.broker.config
        vault = self.app.broker.vault
        lines: list[Line] = []
        focus = 0
        var_width = min(max((len(var) for var in preset.env), default=8) + 2, max(columns // 3, 12))
        secret_width = min(max((len(entry.secret) for entry in preset.env.values()), default=3) + 2, max(columns // 3, 12))
        for index, (kind, var) in enumerate(self.rows()):
            chosen = index == self.selection.index
            if chosen:
                focus = len(lines)
            pointer = Span(f"  {POINTER} " if chosen else "    ", CYAN)
            if kind == "limit":
                lines.append([pointer, Span(f"{'Longest session':<{var_width + secret_width}}", BOLD if chosen else ""), Span(preset.max_session or "no limit of its own", "" if preset.max_session else DIM)])
                lines += [[], styled(f"    {'Variable':<{var_width}}{'Key':<{secret_width}}Approval", DIM)]
            elif kind == "var":
                entry = preset.env[var]
                missing = entry.secret not in vault
                approval = f"{entry.approval} · set here" if entry.approval else approval_text(config, entry.secret)
                lines.append([
                    pointer,
                    Span(f"{var:<{var_width}}", BOLD if chosen else ""),
                    Span(f"{entry.secret:<{secret_width}}", RED if missing else ""),
                    Span("not in the vault" if missing else approval, RED if missing else DIM),
                ])
            else:
                lines.append([pointer, Span(ADD_VARIABLE_ROW, ACCENT if chosen else CYAN)])
        if not preset.env:
            lines += [[], styled("    A preset needs at least one variable before it can be saved.", YELLOW)]
        return window(lines, focus, rows)
