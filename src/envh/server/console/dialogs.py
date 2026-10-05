"""The console's screens and the dialogs they open. A dialog sits in a box at the bottom of the screen and takes the keys
until it closes; Enter always closes a text prompt, so a refused value ends it and nothing typed next lands in it."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from envh.common import peek
from envh.server.console.tui import ACCENT, BOLD, CYAN, DIM, POINTER, Key, Line, Selection, Span, TextField, box, line_width, scrolled, styled, wrap, wrap_line

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp

CLOSE_KEYS = ("escape", "ctrl_c")


class View:
    """A screen of the console. The app draws the header, the status line and the hints around its body."""

    def __init__(self, app: ConsoleApp) -> None:
        self.app = app

    def title(self) -> str:
        return ""

    def body(self, columns: int, rows: int) -> list[Line]:
        return []

    def hints(self) -> str:
        return "Esc back"

    def status(self) -> Line | None:
        return None

    def typing(self) -> bool:
        """True while a text field has the keys; a request then waits instead of taking them over."""
        return False

    def handle(self, key: Key) -> None:
        if key in CLOSE_KEYS:
            self.app.pop()

    def refresh(self, renamed: dict[str, str]) -> None:
        """Reread what the view shows after a change; renamed maps old key names to new ones."""


def text_lines(text: str, columns: int, style: str = "") -> list[Line]:
    return [styled(part, style) for paragraph in text.split("\n") for part in wrap(paragraph, columns)]


class Prompt(View):
    """Asks for one line of text. Enter closes it and hands the text to on_submit, which checks it."""

    def __init__(
        self,
        app: ConsoleApp,
        title: str,
        on_submit: Callable[[str], None],
        *,
        initial: str = "",
        accept: Callable[[str], str] = lambda char: char,
        hidden: bool = False,
        hint: str = "",
        phrase: bool = False,
        limit: int = 4096,
    ) -> None:
        super().__init__(app)
        self.heading = title
        self.on_submit = on_submit
        self.field = TextField(initial, accept=accept, hidden=hidden, limit=limit)
        self.hint = hint
        self.phrase = phrase

    def typing(self) -> bool:
        return True

    def hints(self) -> str:
        return "Enter OK · Esc cancel"

    def handle(self, key: Key) -> None:
        if key in CLOSE_KEYS:
            self.app.close(self)
            self.app.tell("Nothing changed.")
        elif key == "enter":
            self.app.close(self)
            self.on_submit(self.field.text)
        elif key == "tab" and self.field.hidden:
            self.field.insert("\t")
        else:
            self.field.handle(key)

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        lines = text_lines(self.hint, inner, DIM) if self.hint else []
        if lines:
            lines.append([])
        prefix = [Span("> ", BOLD), *([Span(f"[{self.app.phrase}] ", CYAN)] if self.phrase else [])]
        lines.append([*prefix, *self.field.render(inner - line_width(prefix))])
        return box(lines, columns, styled(self.heading, ACCENT))


class PassphraseDialog(View):
    """Asks for the vault passphrase before a change, next to the console phrase. The app checks it and runs the change."""

    def __init__(self, app: ConsoleApp, title: str, details: list[Line], on_accept: Callable[[], None]) -> None:
        super().__init__(app)
        self.heading = title
        self.details = details
        self.on_accept = on_accept
        self.field = TextField(hidden=True, accept=lambda char: "" if char in "\r\n" else char)
        self.offset = 0

    def typing(self) -> bool:
        return True

    def hints(self) -> str:
        return "Type your vault passphrase · Enter confirm · Esc cancel" + (" · PgUp/PgDn scroll" if self.offset or len(self.details) > 8 else "")

    def handle(self, key: Key) -> None:
        if key in CLOSE_KEYS:
            self.app.close(self)
            self.app.tell("Nothing changed.")
        elif key in ("page_up", "page_down"):
            self.offset += 8 if key == "page_down" else -8
        elif key == "enter":
            if self.field.text:
                typed = self.field.text
                self.field.clear()
                self.app.close(self)
                self.app.run_with_passphrase(typed, self.heading, self.on_accept)
        elif key == "tab":
            self.field.insert("\t")
        else:
            self.field.handle(key)

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        details = [part for line in self.details for part in wrap_line(line, inner)]
        visible, self.offset, below = scrolled(details, max(rows - 6, 1), self.offset)
        lines = list(visible)
        if below:
            lines.append(styled(f"↓ {below} more line{'s' if below > 1 else ''} · PgDn", DIM))
        if lines:
            lines.append([])
        lines.append([Span(f"[{self.app.phrase}] ", CYAN), Span("Vault passphrase: ", BOLD), *self.field.render(inner)])
        return box(lines, columns, styled(self.heading, ACCENT))


class Choice(View):
    """A short menu: arrows pick an option, Enter runs it, Esc closes without choosing."""

    def __init__(
        self,
        app: ConsoleApp,
        title: str,
        options: list[tuple[str, Callable[[], None]]],
        lines: list[Line] | None = None,
        selected: int = 0,
        on_cancel: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(app)
        self.heading = title
        self.options = options
        self.lines = lines or []
        self.selection = Selection(selected)
        self.on_cancel = on_cancel
        self.offset = 0

    def hints(self) -> str:
        return "↑↓ choose · Enter OK · Esc cancel"

    def handle(self, key: Key) -> None:
        if key in ("page_up", "page_down"):
            self.offset += 8 if key == "page_down" else -8
        elif self.selection.move(key, len(self.options)):
            return
        elif key in CLOSE_KEYS:
            self.app.close(self)
            if self.on_cancel is not None:
                self.on_cancel()
        elif key == "enter":
            self.app.close(self)
            self.options[self.selection.index][1]()

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        details = [part for line in self.lines for part in wrap_line(line, inner)]
        visible, self.offset, below = scrolled(details, max(rows - len(self.options) - 4, 1), self.offset)
        lines = list(visible)
        if below:
            lines.append(styled(f"↓ {below} more line{'s' if below > 1 else ''} · PgDn", DIM))
        if lines:
            lines.append([])
        for index, (label, _) in enumerate(self.options):
            chosen = index == self.selection.index
            lines.append([Span(f"{POINTER} " if chosen else "  ", CYAN), Span(label, BOLD if chosen else "")])
        return box(lines, columns, styled(self.heading, ACCENT))


class Info(View):
    """Lines to read, such as the shortcuts; Esc, Enter or ? closes it."""

    def __init__(self, app: ConsoleApp, title: str, lines: list[Line]) -> None:
        super().__init__(app)
        self.heading = title
        self.lines = lines
        self.offset = 0

    def hints(self) -> str:
        return "Esc close · ↑↓ scroll"

    def handle(self, key: Key) -> None:
        if key in (*CLOSE_KEYS, "enter", "?"):
            self.app.close(self)
        elif key in ("up", "down", "page_up", "page_down"):
            self.offset += {"up": -1, "down": 1, "page_up": -8, "page_down": 8}[key]

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        lines = [part for line in self.lines for part in wrap_line(line, inner)]
        visible, self.offset, below = scrolled(lines, max(rows - 2, 1), self.offset)
        if below:
            visible = visible[:-1] + [styled(f"↓ {below + 1} more lines", DIM)]
        return box(visible, columns, styled(self.heading, ACCENT))


class KeyPicker(View):
    """Pick a key from the vault; typing narrows the list."""

    def __init__(self, app: ConsoleApp, title: str, names: list[str], on_pick: Callable[[str], None], preselect: str | None = None, hint: str = "") -> None:
        super().__init__(app)
        self.heading = title
        self.names = names
        self.on_pick = on_pick
        self.hint = hint
        self.filter = TextField(accept=lambda char: char.upper() if char.isalnum() or char == "_" else "_" if char in "-. " else "")
        self.selection = Selection(names.index(preselect) if preselect in names else 0)

    def typing(self) -> bool:
        return True

    def hints(self) -> str:
        return "Type to narrow · ↑↓ choose · Enter pick · Esc cancel"

    def matches(self) -> list[str]:
        return [name for name in self.names if self.filter.text in name]

    def handle(self, key: Key) -> None:
        matches = self.matches()
        if self.selection.move(key, len(matches)):
            return
        if key in CLOSE_KEYS:
            self.app.close(self)
            self.app.tell("Nothing changed.")
        elif key == "enter":
            if matches:
                self.app.close(self)
                self.on_pick(matches[self.selection.index])
        elif self.filter.handle(key):
            self.selection.index = 0

    def body(self, columns: int, rows: int) -> list[Line]:
        inner = columns - 4
        matches = self.matches()
        lines = text_lines(self.hint, inner, DIM) if self.hint else []
        lines.append([Span("Filter ", DIM), *self.filter.render(inner - 7)])
        lines.append([])
        room = max(rows - len(lines) - 2, 3)
        if not matches:
            lines.append(styled("No key matches." if self.names else "The vault has no keys yet.", DIM))
        name_width = min(max((len(name) for name in matches), default=0) + 2, max(inner // 2, 12))
        for index in self.selection.visible(len(matches), room):
            name = matches[index]
            chosen = index == self.selection.index
            value = self.app.broker.vault.get(name)
            description = self.app.broker.config.description_for(name) or ""
            lines.append([
                Span(f"{POINTER} " if chosen else "  ", CYAN),
                Span(f"{name:<{name_width}}", BOLD if chosen else ""),
                Span(f"{peek(value):<11}", DIM),
                Span(description, DIM),
            ])
        return box(lines, columns, styled(self.heading, ACCENT))
