"""The console's key list: arrow through the stored keys and rename one with F2. Holds what the screen shows and draws
it; the console reads the keys, checks the passphrase and does the renaming."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from envh.core.config import SECRET_NAME

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


def split_keys(text: str) -> list[str]:
    """The keys in a chunk of terminal input: names like up, f2 or enter, or the typed character. Other function keys are dropped."""
    keys: list[str] = []
    index = 0
    while index < len(text):
        known = next((sequence for sequence in SEQUENCES_LONGEST_FIRST if text.startswith(sequence, index)), None)
        if known is not None:
            keys.append(KEY_SEQUENCES[known])
            index += len(known)
            continue
        other = OTHER_SEQUENCE.match(text, index)
        if other is not None:
            index = other.end()
            continue
        keys.append("escape" if text[index] == "\x1b" else text[index])
        index += 1
    return keys


def name_character(char: str) -> str:
    """What a typed character becomes in a key name: names are UPPER_CASE, and - . or a space become _."""
    if char in "-. ":
        return "_"
    if char.isascii() and (char.isalnum() or char == "_"):
        return char.upper()
    return ""


class KeyList:
    """The key list and its three modes: browsing, typing a new name, and typing the passphrase. The passphrase is asked
    at the first rename of a visit only, so renaming several keys takes one passphrase."""

    def __init__(self, rows: list[KeyRow]) -> None:
        self.rows = rows
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
        """Letters do nothing here, so a passphrase typed at the wrong moment is dropped rather than taken as commands."""
        moves = {"up": -1, "down": 1, "page_up": -PAGE, "page_down": PAGE, "home": -len(self.rows), "end": len(self.rows)}
        if key in moves and self.rows:
            self.selected = min(max(self.selected + moves[key], 0), len(self.rows) - 1)
        elif key in ("f2", "enter") and self.rows:
            self.mode, self.draft = RENAME, self.rows[self.selected].name
        elif key == "escape":
            return Leave()
        return None

    def _edit_name(self, key: str) -> Rename | None:
        if key == "escape":
            self.mode = BROWSE
        elif key == "backspace":
            self.draft = self.draft[:-1]
        elif key == "clear":
            self.draft = ""
        elif key == "enter":
            return self._submit_name()
        elif len(key) == 1:
            self.draft += name_character(key)
        return None

    def _submit_name(self) -> Rename | None:
        old = self.rows[self.selected].name
        if self.draft in ("", old):
            self.mode = BROWSE
        elif not SECRET_NAME.match(self.draft):
            self.tell("Names use A-Z, 0-9 and _, and start with a letter.")
        elif any(row.name == self.draft for row in self.rows):
            self.tell(f"{self.draft} is already taken.")
        elif self.unlocked:
            self.mode = BROWSE
            return Rename(old, self.draft)
        else:
            self.mode, self.typed = PASSPHRASE, ""
        return None

    def _type_passphrase(self, key: str) -> CheckPassphrase | None:
        if key == "escape":
            self.mode = BROWSE
            self.tell("Nothing renamed.")
        elif key == "backspace":
            self.typed = self.typed[:-1]
        elif key == "clear":
            self.typed = ""
        elif key == "enter":
            typed, self.typed = self.typed, ""
            return CheckPassphrase(typed)
        elif len(key) == 1 and key.isprintable():
            self.typed += key
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
    """One frame, drawn from the top-left corner over the last one so the screen does not flicker."""
    width, height = size.columns, size.lines
    visible = max(height - 9, 3)
    count = f"{len(view.rows)} key" + ("" if len(view.rows) == 1 else "s")
    lines = ["", f"  {paint('1', 'Keys')}  {paint('2', count)}", ""]
    if not view.rows:
        lines.append(paint("2", "    No keys yet. Import some with: envh import <folder>"))
    else:
        name_width = max(len(row.name) for row in view.rows) + 2
        if view.mode != BROWSE:
            name_width = max(name_width, len(view.draft) + 3)
        name_width = min(name_width, max(width // 2, 12))
        lines.append(paint("2", f"    {'Name':<{name_width}}Used by"))
        top = min(max(view.selected - visible // 2, 0), max(len(view.rows) - visible, 0))
        for index in range(top, min(top + visible, len(view.rows))):
            lines.append(render_row(view, index, name_width, width))
    lines.append("")
    if waiting:
        lines.append(paint("33", f"  ! {waiting} request{'s' if waiting > 1 else ''} waiting. Press Esc to answer."))
    if view.message:
        mark, color = {"ok": ("✓", "32"), "warn": ("!", "33"), "info": ("·", "2")}[view.message_style]
        lines.append(paint(color, f"  {mark} {view.message}"))
    if view.mode == BROWSE:
        lines.append(paint("2", "  ↑↓ move   F2 rename   Esc back"))
    elif view.mode == RENAME:
        lines.append(paint("2", "  Type the new name   Enter save   Esc cancel"))
    else:
        lines.append(f"  [{phrase}] vault passphrase to rename (hidden): ")
    return "\x1b[H" + "\n".join(line + "\x1b[K" for line in lines[: height - 1]) + "\x1b[J"


def render_row(view: KeyList, index: int, name_width: int, width: int) -> str:
    row = view.rows[index]
    used_by = ", ".join(row.used_by) or "-"
    details = fit(used_by + ("   per-run" if row.per_run else ""), max(width - name_width - 6, 10))
    if index != view.selected:
        return f"    {fit(row.name, name_width - 2):<{name_width}}{paint('2', details)}"
    if view.mode == BROWSE:
        return f"  {paint('36', '›')} {paint('1', f'{fit(row.name, name_width - 2):<{name_width}}')}{paint('2', details)}"
    draft = fit(view.draft, name_width - 3)
    cursor = "▏" if view.mode == RENAME else " "
    return f"  {paint('36', '›')} {paint('1;4', draft)}{cursor}{' ' * (name_width - len(draft) - 1)}{paint('2', 'was ' + row.name)}"
