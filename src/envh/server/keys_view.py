"""A key list screen: arrow through key names and rename one with F2. KeyList holds the screen's state and turns key
presses into actions such as Rename; render_keys draws one frame of it."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass

KEY_SEQUENCES = {
    "\x1b[A": "up",
    "\x1bOA": "up",
    "\x1b[B": "down",
    "\x1bOB": "down",
    "\x1b[5~": "page_up",
    "\x1b[6~": "page_down",
    "\x1b[H": "home",
    "\x1bOH": "home",
    "\x1b[1~": "home",
    "\x1b[F": "end",
    "\x1bOF": "end",
    "\x1b[4~": "end",
    "\x1bOQ": "f2",
    "\x1b[12~": "f2",
    "\x1b[[B": "f2",
    "\r": "enter",
    "\n": "enter",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x15": "clear",
    "\x03": "escape",
}
SEQUENCES_LONGEST_FIRST = sorted(KEY_SEQUENCES, key=len, reverse=True)
OTHER_SEQUENCE = re.compile(r"\x1b(\[\[.|\[[0-?]*[ -/]*[@-~]|O.)", re.DOTALL)
INCOMPLETE_SEQUENCE = re.compile(r"\x1b(\[\[?[0-?]*[ -/]*|O)?$")
ENTER_FULL_SCREEN = "\x1b[?1049h\x1b[?25l"
LEAVE_FULL_SCREEN = "\x1b[?25h\x1b[?1049l"
PAGE = 10
BROWSE, RENAME, PASSPHRASE = "browse", "rename", "passphrase"


@dataclass(frozen=True)
class KeyRow:
    name: str
    used_by: tuple[str, ...]
    per_run: bool


@dataclass(frozen=True)
class Leave:
    pass


@dataclass(frozen=True)
class CheckPassphrase:
    typed: str


@dataclass(frozen=True)
class Rename:
    old: str
    new: str


def key_spans(text: str) -> list[tuple[str, int]]:
    """Each key in a chunk of terminal input with the offset just past it: a name like up, f2 or enter, or the typed
    character. Other function keys are dropped."""
    spans: list[tuple[str, int]] = []
    index = 0
    while index < len(text):
        known = next((sequence for sequence in SEQUENCES_LONGEST_FIRST if text.startswith(sequence, index)), None)
        if known is not None:
            index += len(known)
            spans.append((KEY_SEQUENCES[known], index))
            continue
        other = OTHER_SEQUENCE.match(text, index)
        if other is not None:
            index = other.end()
            continue
        spans.append(("escape" if text[index] == "\x1b" else text[index], index + 1))
        index += 1
    return spans


def split_keys(text: str) -> list[str]:
    return [key for key, _ in key_spans(text)]


def incomplete_sequence(text: str) -> bool:
    """True when text ends partway into an escape sequence, so the rest of the key is still on its way."""
    return INCOMPLETE_SEQUENCE.search(text) is not None


def name_character(char: str) -> str:
    """What a typed character becomes in a key name: names are UPPER_CASE, and - . or a space become _."""
    if char in "-. ":
        return "_"
    if char.isascii() and (char.isalnum() or char == "_"):
        return char.upper()
    return ""


def edited(text: str, key: str, accept: Callable[[str], str]) -> str:
    if key == "backspace":
        return text[:-1]
    if key == "clear":
        return ""
    if len(key) == 1:
        return text + accept(key)
    return text


class KeyList:
    """The key list and its three modes: browsing, typing a new name, and typing the passphrase. check_name says why a
    rename would be refused, or returns None. The passphrase is asked at the first rename of a visit only, so renaming
    several keys takes one passphrase."""

    def __init__(self, rows: list[KeyRow], check_name: Callable[[str, str], str | None]) -> None:
        self.rows = rows
        self.check_name = check_name
        self.selected = 0
        self.mode = BROWSE
        self.draft = ""
        self.typed = ""
        self.unlocked = False
        self.message = ""
        self.message_style = "warn"

    def handle(self, key: str) -> Leave | CheckPassphrase | Rename | None:
        self.message = ""
        if self.mode == RENAME:
            return self._edit_name(key)
        if self.mode == PASSPHRASE:
            return self._type_passphrase(key)
        return self._browse(key)

    def _browse(self, key: str) -> Leave | None:
        """Only arrows, F2 and Esc do anything here, so a passphrase typed at the wrong moment, Enter included, is dropped."""
        moves = {"up": -1, "down": 1, "page_up": -PAGE, "page_down": PAGE, "home": -len(self.rows), "end": len(self.rows)}
        if key in moves and self.rows:
            self.selected = min(max(self.selected + moves[key], 0), len(self.rows) - 1)
        elif key == "f2" and self.rows:
            self.mode, self.draft = RENAME, self.rows[self.selected].name
        elif key == "escape":
            return Leave()
        return None

    def _edit_name(self, key: str) -> Rename | None:
        if key == "escape":
            self.mode = BROWSE
        elif key == "enter":
            return self._submit_name()
        else:
            self.draft = edited(self.draft, key, name_character)
        return None

    def _submit_name(self) -> Rename | None:
        """A refused name ends the edit, so whatever is typed next cannot land in the visible name field."""
        old = self.rows[self.selected].name
        problem = None if self.draft in ("", old) else self.check_name(old, self.draft)
        if self.draft in ("", old) or problem is not None:
            self.mode = BROWSE
            if problem is not None:
                self.tell(problem)
            return None
        if self.unlocked:
            self.mode = BROWSE
            return Rename(old, self.draft)
        self.mode, self.typed = PASSPHRASE, ""
        return None

    def _type_passphrase(self, key: str) -> CheckPassphrase | None:
        if key == "escape":
            self.mode = BROWSE
            self.tell("Nothing renamed.")
        elif key == "enter":
            typed, self.typed = self.typed, ""
            return CheckPassphrase(typed)
        else:
            self.typed = edited(self.typed, key, lambda char: char)
        return None

    def passphrase_accepted(self) -> Rename:
        self.unlocked = True
        self.mode = BROWSE
        return Rename(self.rows[self.selected].name, self.draft)

    def passphrase_rejected(self) -> None:
        self.mode = BROWSE
        self.tell("Wrong passphrase. Nothing renamed.")

    def renamed(self, rows: list[KeyRow], new: str) -> None:
        self.rows = rows
        self.selected = next((index for index, row in enumerate(rows) if row.name == new), 0)
        self.tell(f"Renamed to {new}.", "ok")

    def tell(self, message: str, style: str = "warn") -> None:
        """Show a one-line message under the list until the next key; style is ok, warn or info."""
        self.message, self.message_style = message, style


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def render_keys(view: KeyList, phrase: str, waiting: int, size: os.terminal_size) -> str:
    """One frame, drawn from the top-left corner over the last one so the screen does not flicker. On a short screen the
    list gives way first, so the hints and the passphrase prompt with its phrase always show."""
    width, height = size.columns, size.lines
    footer: list[str] = []
    if waiting:
        footer.append(paint("33", f"  ! {waiting} request{'s' if waiting > 1 else ''} waiting. Press Esc to answer."))
    if view.message:
        mark, color = {"ok": ("✓", "32"), "warn": ("!", "33"), "info": ("·", "2")}[view.message_style]
        footer.append(paint(color, f"  {mark} {view.message}"))
    if view.mode == BROWSE:
        footer.append(paint("2", "  ↑↓ move   F2 rename   Esc back"))
    elif view.mode == RENAME:
        footer.append(paint("2", "  Type the new name   Enter save   Esc cancel"))
    else:
        footer.append(f"  [{phrase}] vault passphrase to rename (hidden): ")
    count = f"{len(view.rows)} key" + ("" if len(view.rows) == 1 else "s")
    lines = ["", f"  {paint('1', 'Keys')}  {paint('2', count)}", ""]
    if not view.rows:
        lines.append(paint("2", "    No keys yet. Import some with: envh import <folder>"))
    else:
        visible = max(height - len(lines) - len(footer) - 3, 1)
        name_width = max(len(row.name) for row in view.rows) + 2
        if view.mode != BROWSE:
            name_width = max(name_width, len(view.draft) + 3)
        name_width = min(name_width, max(width // 2, 12))
        lines.append(paint("2", f"    {'Name':<{name_width}}Used by"))
        top = min(max(view.selected - visible // 2, 0), max(len(view.rows) - visible, 0))
        lines.extend(render_row(view, index, name_width, width) for index in range(top, min(top + visible, len(view.rows))))
    lines.append("")
    lines = lines[: max(height - 1 - len(footer), 0)] + footer
    return "\x1b[H" + "\n".join(line + "\x1b[K" for line in lines) + "\x1b[J"


def render_row(view: KeyList, index: int, name_width: int, width: int) -> str:
    row = view.rows[index]
    selected = index == view.selected
    pointer = paint("36", "›") if selected else " "
    details_width = max(width - name_width - 6, 10)
    if selected and view.mode != BROWSE:
        draft = fit(view.draft, name_width - 3)
        cursor = "▏" if view.mode == RENAME else " "
        padding = " " * (name_width - len(draft) - 1)
        return f"  {pointer} {paint('1;4', draft)}{cursor}{padding}{paint('2', fit('was ' + row.name, details_width))}"
    name = f"{fit(row.name, name_width - 2):<{name_width}}"
    details = fit((", ".join(row.used_by) or "-") + ("   per-run" if row.per_run else ""), details_width)
    return f"  {pointer} {paint('1', name) if selected else name}{paint('2', details)}"
