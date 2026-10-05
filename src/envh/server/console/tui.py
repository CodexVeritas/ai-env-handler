"""Terminal building blocks for the console, standard library only: key input, styled lines that fit the screen, boxes,
text fields, scrolling lists, and the full-screen mode with its restore."""

from __future__ import annotations

import os
import re
import termios
import tty
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from envh.common import printable

BOLD = "1"
DIM = "2"
INVERSE = "7"
ACCENT = "1;36"
CYAN = "36"
GREEN = "32"
YELLOW = "33"
RED = "31"
MARKS = {"ok": ("✓", GREEN), "warn": ("!", YELLOW), "error": ("✗", RED), "info": ("·", DIM)}
POINTER = "›"
BELL = "\a"
ENTER_FULL_SCREEN = "\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[2J"
LEAVE_FULL_SCREEN = "\x1b[?2004l\x1b[0m\x1b[?25h\x1b[?1049l"
DEFAULT_SIZE = (80, 24)

KEY_NAMES = {
    "\x1b[A": "up",
    "\x1bOA": "up",
    "\x1b[B": "down",
    "\x1bOB": "down",
    "\x1b[C": "right",
    "\x1bOC": "right",
    "\x1b[D": "left",
    "\x1bOD": "left",
    "\x1b[5~": "page_up",
    "\x1b[6~": "page_down",
    "\x1b[H": "home",
    "\x1bOH": "home",
    "\x1b[1~": "home",
    "\x1b[7~": "home",
    "\x1b[F": "end",
    "\x1bOF": "end",
    "\x1b[4~": "end",
    "\x1b[8~": "end",
    "\x1b[3~": "delete",
    "\x1bOQ": "f2",
    "\x1b[12~": "f2",
    "\x1b[[B": "f2",
    "\x1b[Z": "shift_tab",
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x7f": "backspace",
    "\x08": "backspace",
    "\x01": "ctrl_a",
    "\x03": "ctrl_c",
    "\x05": "ctrl_e",
    "\x0c": "ctrl_l",
    "\x13": "ctrl_s",
    "\x15": "ctrl_u",
    "\x17": "ctrl_w",
}
SEQUENCES_LONGEST_FIRST = sorted(KEY_NAMES, key=len, reverse=True)
OTHER_SEQUENCE = re.compile(r"\x1b(\[\[.|\[[0-?]*[ -/]*[@-~]|O[A-DFHP-S])", re.DOTALL)
INCOMPLETE_SEQUENCE = re.compile(r"\x1b(\[\[?[0-?]*[ -/]*|O)?")
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


@dataclass(frozen=True)
class Span:
    text: str
    style: str = ""


Line = list[Span]


@dataclass(frozen=True)
class Paste:
    text: str


Key = str | Paste


def styled(text: str, style: str = "") -> Line:
    return [Span(text, style)]


def mark(kind: str, text: str, style: str = "") -> Line:
    """A status line part: ✓ done, ! needs attention, ✗ stopped, · neutral."""
    symbol, color = MARKS[kind]
    return [Span(symbol, color), Span(" " + text, style)]


def typed_character(char: str) -> bool:
    """Whether a character is text someone typed, as opposed to a control code."""
    return char >= " " and char != "\x7f" and not "\x80" <= char <= "\x9f"


def char_width(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def line_width(line: Line) -> int:
    return sum(text_width(span.text) for span in line)


def take(text: str, columns: int) -> str:
    """The longest start of text that fits in columns."""
    kept, used = [], 0
    for char in text:
        used += char_width(char)
        if used > columns:
            break
        kept.append(char)
    return "".join(kept)


def take_end(text: str, columns: int) -> str:
    """The longest end of text that fits in columns."""
    return take(text[::-1], columns)[::-1]


def clip(text: str, columns: int) -> str:
    """text cut to columns, ending in … where it was cut."""
    if text_width(text) <= columns:
        return text
    return take(text, columns - 1) + "…" if columns > 0 else ""


def fit(line: Line, columns: int) -> Line:
    """line cut to columns, keeping each part's style and ending in … where it was cut."""
    if line_width(line) <= columns:
        return line
    fitted: Line = []
    room = columns - 1
    for span in line:
        width = text_width(span.text)
        if width <= room:
            fitted.append(span)
            room -= width
            continue
        fitted.append(Span(take(span.text, room) + "…", span.style))
        break
    return fitted if columns > 0 else []


def pad(line: Line, columns: int) -> Line:
    fitted = fit(line, columns)
    return fitted + [Span(" " * (columns - line_width(fitted)))]


def plain(line: Line) -> str:
    return "".join(span.text for span in line)


def safe(line: Line) -> Line:
    """line with every character that could move the cursor, restyle the screen or reorder text replaced."""
    return [Span(printable(span.text), span.style) for span in line]


def render(line: Line, columns: int, color: bool) -> str:
    parts = []
    for span in fit(safe(line), columns):
        parts.append(f"\x1b[{span.style}m{span.text}\x1b[0m" if color and span.style and span.text else span.text)
    return "".join(parts)


def wrap(text: str, columns: int) -> list[str]:
    """text broken into lines of at most columns, at spaces where it can and mid-word where a word is too long."""
    columns = max(columns, 1)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}" if current else word
        if text_width(candidate) <= columns:
            current = candidate
            continue
        if current:
            lines.append(current)
        while text_width(word) > columns:
            head = take(word, columns)
            lines.append(head)
            word = word[len(head):]
        current = word
    lines.append(current)
    return lines


def wrap_line(line: Line, columns: int) -> list[Line]:
    """A styled line broken into lines of at most columns; each part keeps its style."""
    if line_width(line) <= columns:
        return [line]
    if len(line) == 1:
        return [[Span(text, line[0].style)] for text in wrap(line[0].text, columns)]
    rows: list[Line] = [[]]
    used = 0
    for span in line:
        text = span.text
        while text:
            piece = take(text, columns - used) or (text[0] if used == 0 else "")
            if not piece:
                rows.append([])
                used = 0
                continue
            rows[-1].append(Span(piece, span.style))
            used += text_width(piece)
            text = text[len(piece):]
    return rows


def box(body: list[Line], columns: int, title: Line | None = None, right: Line | None = None, border: str = DIM) -> list[Line]:
    """body inside a rounded border, with an optional title on the left of the top edge and text on its right."""
    inner = max(columns - 4, 1)
    left_part = [Span(" ")] + fit(title, inner - 2) + [Span(" ")] if title else []
    right_part = [Span(" ")] + right + [Span(" ")] if right else []
    fill = columns - 4 - line_width(left_part) - line_width(right_part)
    if fill < 1:
        right_part, fill = [], columns - 4 - line_width(left_part)
    top = [Span("╭─", border), *left_part, Span("─" * max(fill, 0), border), *right_part, Span("─╮", border)]
    rows = [[Span("│ ", border), *pad(line, inner), Span(" │", border)] for line in body]
    bottom = [Span("╰" + "─" * max(columns - 2, 0) + "╯", border)]
    return [top, *rows, bottom]


def window(lines: list[Line], focus: int, rows: int) -> list[Line]:
    """The part of lines that fits in rows, chosen so line focus is on screen."""
    if len(lines) <= rows:
        return lines
    start = min(max(focus - rows // 2, 0), len(lines) - rows)
    return lines[start:start + rows]


def scrolled(lines: list[Line], rows: int, offset: int) -> tuple[list[Line], int, int]:
    """The part of lines that fits in rows starting offset lines down, with the offset kept in range and how many lines
    are left below it."""
    offset = min(max(offset, 0), max(len(lines) - rows, 0))
    visible = lines[offset:offset + rows]
    return visible, offset, len(lines) - offset - len(visible)


class TextField:
    """One line of text being typed, with a cursor. accept turns each typed character into what the field keeps ("" drops
    it). A hidden field never shows what it holds, not even its length."""

    def __init__(self, text: str = "", accept: Callable[[str], str] = lambda char: char, hidden: bool = False, limit: int = 4096) -> None:
        self.text = ""
        self.cursor = 0
        self.accept = accept
        self.hidden = hidden
        self.limit = limit
        self.insert(text)

    def insert(self, text: str) -> None:
        for char in text:
            kept = self.accept(char)
            if kept and len(self.text) + len(kept) <= self.limit:
                self.text = self.text[:self.cursor] + kept + self.text[self.cursor:]
                self.cursor += len(kept)

    def clear(self) -> None:
        self.text, self.cursor = "", 0

    def handle(self, key: Key) -> bool:
        """Apply an editing key; False when the key is not one."""
        if isinstance(key, Paste):
            self.insert(key.text.rstrip("\r\n"))
        elif key == "backspace":
            if self.cursor:
                self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
                self.cursor -= 1
        elif key == "delete":
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        elif key == "left":
            self.cursor = max(self.cursor - 1, 0)
        elif key == "right":
            self.cursor = min(self.cursor + 1, len(self.text))
        elif key in ("home", "ctrl_a"):
            self.cursor = 0
        elif key in ("end", "ctrl_e"):
            self.cursor = len(self.text)
        elif key == "ctrl_u":
            self.clear()
        elif key == "ctrl_w":
            start = len(self.text[:self.cursor].rstrip()) - len(self.text[:self.cursor].rstrip().split(" ")[-1])
            self.text, self.cursor = self.text[:start] + self.text[self.cursor:], start
        elif len(key) == 1 and typed_character(key):
            self.insert(key)
        else:
            return False
        return True

    def render(self, columns: int, style: str = "") -> Line:
        if self.hidden:
            return [Span("•••", CYAN), Span(" (hidden)", DIM)] if self.text else [Span("(hidden)", DIM)]
        before, after = self.text[:self.cursor], self.text[self.cursor:]
        if text_width(before) > columns - 2:
            before = "…" + take_end(before, columns - 3)
        return [Span(before, style), Span(after[:1] or " ", INVERSE), Span(after[1:], style)]


class Selection:
    """The selected row of a list and the first row on screen, so the selection stays in view as the list scrolls."""

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.top = 0

    def move(self, key: Key, count: int, page: int = 10) -> bool:
        """Apply a moving key (arrows, PgUp/PgDn, Home/End); False when the key is not one."""
        steps = {"up": -1, "down": 1, "page_up": -page, "page_down": page, "home": -count, "end": count}
        if not isinstance(key, str) or key not in steps:
            return False
        if count:
            self.index = min(max(self.index + steps[key], 0), count - 1)
        return True

    def visible(self, count: int, rows: int) -> range:
        rows = max(rows, 1)
        self.index = min(max(self.index, 0), max(count - 1, 0))
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + rows:
            self.top = self.index - rows + 1
        self.top = min(self.top, max(count - rows, 0))
        return range(self.top, min(self.top + rows, count))


class KeyReader:
    """Turns terminal input into keys: names like up, f2 or ctrl_s, typed characters, and pastes. A sequence cut off at
    the end of a read waits for the next one; flush() settles it when nothing more came, so a lone Esc is the Esc key.
    Function keys it does not know are dropped."""

    def __init__(self) -> None:
        self._pending = ""
        self._paste: list[str] | None = None

    @property
    def waiting(self) -> bool:
        return bool(self._pending) or self._paste is not None

    def feed(self, text: str) -> list[Key]:
        data, self._pending = self._pending + text, ""
        keys: list[Key] = []
        index = 0
        while index < len(data):
            if self._paste is not None:
                end = data.find(PASTE_END, index)
                if end < 0:
                    kept = next((size for size in range(len(PASTE_END) - 1, 0, -1) if data.endswith(PASTE_END[:size])), 0)
                    self._paste.append(data[index:len(data) - kept])
                    self._pending = data[len(data) - kept:]
                    return keys
                self._paste.append(data[index:end])
                keys.append(Paste("".join(self._paste)))
                self._paste = None
                index = end + len(PASTE_END)
            elif data.startswith(PASTE_START, index):
                self._paste = []
                index += len(PASTE_START)
            elif INCOMPLETE_SEQUENCE.fullmatch(data, index):
                self._pending = data[index:]
                return keys
            else:
                index = self._next_key(data, index, keys)
        return keys

    def _next_key(self, data: str, index: int, keys: list[Key]) -> int:
        known = next((sequence for sequence in SEQUENCES_LONGEST_FIRST if data.startswith(sequence, index)), None)
        if known is not None:
            keys.append(KEY_NAMES[known])
            return index + len(known)
        other = OTHER_SEQUENCE.match(data, index)
        if other is not None:
            return other.end()
        char = data[index]
        if char == "\x1b":
            keys.append("escape")
        elif typed_character(char):
            keys.append(char)
        return index + 1

    def flush(self) -> list[Key]:
        pending, self._pending = self._pending, ""
        if self._paste is not None:
            text, self._paste = "".join(self._paste) + pending, None
            return [Paste(text)]
        if not pending:
            return []
        return ["escape", *(char for char in pending[1:] if typed_character(char))]


class Display:
    """Draws frames over the previous one, rewriting only the rows that changed, so the screen does not flicker."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._rows: list[str] = []
        self._size: tuple[int, int] | None = None

    def draw(self, rows: list[str], size: tuple[int, int]) -> None:
        parts = []
        if size != self._size:
            self._rows, self._size = [], size
            parts.append("\x1b[2J")
        height = size[1]
        rows = (rows + [""] * height)[:height]
        for index, row in enumerate(rows):
            if index >= len(self._rows) or self._rows[index] != row:
                parts.append(f"\x1b[{index + 1};1H\x1b[2K{row}")
        self._rows = rows
        if parts:
            self._write("".join(parts))

    def invalidate(self) -> None:
        self._rows, self._size = [], None


class Terminal:
    """The console's terminal: keys are read from tty_fd, whose modes change while the console runs; output goes to
    write. The modes are put back on the way out, also when the terminal has gone away."""

    def __init__(self, tty_fd: int, write: Callable[[str], None]) -> None:
        self.tty_fd = tty_fd
        self.write = write
        self._saved: list | None = None

    def size(self) -> tuple[int, int]:
        """The window's size, or 80x24 when it has none (a terminal never given a size reports 0x0)."""
        try:
            size = os.get_terminal_size(self.tty_fd)
        except OSError:
            return DEFAULT_SIZE
        return (size.columns, size.lines) if size.columns > 0 and size.lines > 0 else DEFAULT_SIZE

    @contextmanager
    def full_screen(self) -> Iterator[None]:
        """Keys arrive one by one, unechoed, on the alternate screen, so the scrollback comes back unchanged. Ctrl-C and
        Ctrl-Z arrive as keys rather than signals, so they cannot stop the broker, and Ctrl-S reaches the console instead
        of pausing the terminal."""
        self._saved = termios.tcgetattr(self.tty_fd)
        raw = list(self._saved)
        raw[tty.CC] = list(self._saved[tty.CC])
        tty.cfmakecbreak(raw)
        raw[tty.LFLAG] &= ~(termios.ISIG | termios.IEXTEN)
        raw[tty.IFLAG] &= ~termios.IXON
        termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, raw)
        self.write(ENTER_FULL_SCREEN)
        try:
            yield
        finally:
            self._restore(self._saved)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """The terminal as it was before the console took it over, for another program such as an editor."""
        current = termios.tcgetattr(self.tty_fd)
        if self._saved is not None:
            self._restore(self._saved)
        try:
            yield
        finally:
            termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, current)
            self.write(ENTER_FULL_SCREEN)

    def _restore(self, modes: list) -> None:
        try:
            self.write(LEAVE_FULL_SCREEN)
            termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, modes)
        except (OSError, termios.error):
            pass
