"""The console app: one full-screen terminal where requests are approved and keys, presets and settings are managed.

A waiting request takes over the screen as soon as nothing else is being typed, and then takes every key until it is
answered, always with the vault passphrase. A change asks for the passphrase too, and for an hour afterwards only for a
confirmation; changing the passphrase itself always asks. A wrong passphrase is audited and stops all input for two
seconds, so guessing by typing blind is slow and visible. Typing on a screen without a text field does nothing, and neither does the
Enter after it, so a passphrase typed at the wrong moment never opens a field that typing it again would fill."""

from __future__ import annotations

import asyncio
import base64
import codecs
import math
import time
import traceback
from collections.abc import Callable, Iterator

from envh.common import reminder_delays
from envh.core.broker import Broker
from envh.core.state import Request
from envh.server.console.activity import Activity
from envh.server.console.dialogs import Choice, PassphraseDialog, View
from envh.server.console.home import Command, HomeView
from envh.server.console.request_card import RequestCard
from envh.server.console.summaries import plural
from envh.server.console.tui import ACCENT, BELL, BOLD, DIM, YELLOW, Display, Key, KeyReader, Line, Paste, Span, Terminal, line_width, mark, render, styled
from envh.server.control import HandledErrors

REFRESH_SECONDS = 0.5
ESCAPE_WAIT_SECONDS = 0.05
WRONG_PASSPHRASE_PAUSE_SECONDS = 2
QUIT_CONFIRM_SECONDS = 2
HISTORY_KEPT = 100
UNLOCK_SECONDS = 3600
LOCK_WARNING_SECONDS = 300
CLIPBOARD_SECONDS = 30
MIN_COLUMNS = 50
MIN_ROWS = 14


def stray(key: Key) -> bool:
    """Typing that a screen without a text field does nothing with; / and ? are the keys it does use."""
    return isinstance(key, Paste) or (len(key) == 1 and key not in "/?")


class ConsoleApp:
    def __init__(
        self,
        broker: Broker,
        phrase: str,
        activity: Activity,
        write: Callable[[str], None] = lambda text: None,
        color: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.broker = broker
        self.phrase = phrase
        self.activity = activity
        self._write = write
        self.color = color
        self.clock = clock
        self.views: list[View] = []
        self.dialogs: list[View] = []
        self.card: RequestCard | None = None
        self.waiting: list[Request] = []
        self.history: list[str] = []
        self.message: tuple[str, str] | None = None
        self.paused_until = 0.0
        self.unlocked_until = 0.0
        self.quit_requested = asyncio.Event()
        self.wake = asyncio.Event()
        self.terminal: Terminal | None = None
        self.display: Display | None = None
        self._quit_armed_until = 0.0
        self._typed_while_browsing = False
        self._lock_warned = False
        self._clear_clipboard_at = 0.0
        self._reminder: tuple[Request, float, Iterator[float]] | None = None
        self._last_failure = ""
        self.views.append(HomeView(self))
        activity.on_change = self.wake.set
        broker.request_listeners.append(self.on_request)

    def on_request(self, request: Request) -> None:
        """A new request, or one shown again because what it would change has changed."""
        if self.card is not None and self.card.request is request:
            self.card.offset = 0
        elif request not in self.waiting:
            self.waiting.append(request)
            request.decision.add_done_callback(lambda _: self._decided(request))
            self.ring()
        self.present()
        self.wake.set()

    def _decided(self, request: Request) -> None:
        withdrawn = not request.decision.cancelled() and request.decision.result().outcome == "withdrawn"
        if self.card is not None and self.card.request is request and withdrawn:
            self.card.withdrawn = True
        self.present()
        self.wake.set()

    def present(self) -> None:
        """Show the oldest waiting request, unless something is being typed or a dialog is open; until then the status
        line says it waits."""
        self.waiting = [request for request in self.waiting if request.pending]
        if self.card is None and not self.dialogs and not self.views[-1].typing() and self.waiting:
            self.card = RequestCard(self, self.waiting.pop(0))

    def approve(self, card: RequestCard, typed: str) -> None:
        request = card.request
        if not request.pending:
            card.withdrawn = True
            return
        if not self.passphrase_matches(typed, f"approve #{request.id}"):
            card.problem = "Wrong passphrase. The request still waits; try again."
            return
        try:
            self.broker.approve(request)
        except (*HandledErrors, OSError) as error:
            card.problem = str(error)
            return
        self.card = None
        self.changed()
        self.tell(f"Approved #{request.id}.", "ok")
        self.present()

    def deny(self, card: RequestCard) -> None:
        request = card.request
        if request.pending:
            self.broker.deny(request)
            self.tell(f"Denied #{request.id}.")
        self.dismiss_card()

    def dismiss_card(self) -> None:
        self.card = None
        self.present()

    def passphrase_matches(self, typed: str, action: str) -> bool:
        if self.broker.vault.matches_passphrase(typed):
            return True
        self.broker.audit.event("wrong_passphrase", action=action)
        self.paused_until = self.clock() + WRONG_PASSPHRASE_PAUSE_SECONDS
        return False

    @property
    def unlocked(self) -> bool:
        return self.clock() < self.unlocked_until

    def ask_passphrase(self, title: str, details: list[Line], action: Callable[[], None], always: bool = False) -> None:
        """Ask for the vault passphrase, then run action, which makes the change. While changes are unlocked, ask only to
        confirm, unless always."""
        if self.unlocked and not always:
            options = [(title, lambda: self._change(action)), ("Cancel", lambda: self.tell("Nothing changed."))]
            self.open(Choice(self, title, options, lines=details, on_cancel=lambda: self.tell("Nothing changed.")))
        else:
            self.open(PassphraseDialog(self, title, details, action))

    def run_with_passphrase(self, typed: str, title: str, action: Callable[[], None]) -> None:
        if not self.passphrase_matches(typed, title):
            self.tell("Wrong passphrase. Nothing changed.", "error")
            return
        if not self.unlocked:
            self.activity.note("info", "Changes unlocked for an hour; /lock locks them now")
        self.unlocked_until = self.clock() + UNLOCK_SECONDS
        self._lock_warned = False
        self._change(action)

    def lock(self, note: str) -> None:
        """End the unlocked hour now; the next change asks for the passphrase again."""
        self.unlocked_until = 0.0
        self.activity.note("info", note)

    def copy_to_clipboard(self, value: str) -> None:
        """Ask the terminal to put value on the clipboard (OSC 52), and to clear it after CLIPBOARD_SECONDS. Terminals
        that don't allow programs to set the clipboard ignore both."""
        self._write(f"\x1b]52;c;{base64.b64encode(value.encode()).decode()}\a")
        self._clear_clipboard_at = self.clock() + CLIPBOARD_SECONDS

    def _change(self, action: Callable[[], None]) -> None:
        working = ("info", "Saving…")
        self.message = working
        self.redraw()
        self.attempt(action)
        if self.message is working:
            self.message = None

    def attempt(self, action: Callable[[], None]) -> bool:
        """Run action; a refusal or a failed write is shown on the status line."""
        try:
            action()
        except (*HandledErrors, OSError) as error:
            self.tell(str(error), "error")
            return False
        return True

    def tell(self, text: str, kind: str = "info") -> None:
        """Show text on the status line until the next key."""
        self.message = (kind, text)

    def push(self, view: View) -> None:
        self.views.append(view)

    def pop(self, view: View | None = None) -> None:
        target = view or self.views[-1]
        if target in self.views[1:]:
            self.views.remove(target)

    def open(self, dialog: View) -> None:
        self.dialogs.append(dialog)

    def close(self, dialog: View) -> None:
        if dialog in self.dialogs:
            self.dialogs.remove(dialog)

    def overlay_open(self) -> bool:
        return self.card is not None or bool(self.dialogs)

    def changed(self, renamed: dict[str, str] | None = None) -> None:
        """Have every open screen reread what it shows."""
        for view in list(self.views):
            view.refresh(renamed or {})

    def run_command(self, command: Command) -> None:
        entry = "/" + command.name
        if self.history[-1:] != [entry]:
            self.history = [*self.history, entry][-HISTORY_KEPT:]
        command.run(self)

    def request_quit(self) -> None:
        if self.clock() < self._quit_armed_until:
            self.stop()
            return
        self._quit_armed_until = self.clock() + QUIT_CONFIRM_SECONDS
        self.tell("Press Ctrl-C again to stop the console. Live sessions end.", "warn")

    def stop(self) -> None:
        self.quit_requested.set()
        self.wake.set()

    def ring(self) -> None:
        if not self.broker.config.notify:
            return
        try:
            self._write(BELL)
        except OSError:
            self.stop()

    def press(self, key: Key) -> None:
        if self.clock() < self.paused_until:
            return
        if key == "ctrl_l":
            if self.display is not None:
                self.display.invalidate()
            return
        self.message = None
        browsing = self.card is None and not self.dialogs and not self.views[-1].typing()
        target = self.card or (self.dialogs[-1] if self.dialogs else self.views[-1])
        if browsing and key == "enter" and self._typed_while_browsing:
            self.tell("Typing does nothing here, so that Enter did nothing either. Use ↑↓, then Enter.")
        else:
            try:
                target.handle(key)
            except Exception:
                self._failed("Something went wrong in the console; the broker keeps running. Details are in audit.jsonl.")
        self._typed_while_browsing = browsing and stray(key)
        self.present()

    def tick(self) -> None:
        """Timed work: ring again, with growing gaps, while the oldest request waits; warn before changes lock again and
        lock them; clear a copied value from the clipboard; close dialogs whose time is up."""
        self._lock_on_time()
        if self._clear_clipboard_at and self.clock() >= self._clear_clipboard_at:
            self._clear_clipboard()
        for dialog in [dialog for dialog in self.dialogs if dialog.expired()]:
            self.close(dialog)
        if self.card is not None and not self.card.withdrawn:
            target: Request | None = self.card.request
        else:
            target = next((request for request in self.waiting if request.pending), None)
        if target is None:
            self._reminder = None
        elif self._reminder is None or self._reminder[0] is not target:
            delays = reminder_delays()
            self._reminder = (target, self.clock() + next(delays), delays)
        elif self.clock() >= self._reminder[1]:
            self.ring()
            self._reminder = (target, self.clock() + next(self._reminder[2]), self._reminder[2])

    def _clear_clipboard(self) -> None:
        """Ask the terminal to empty the clipboard a copy filled. A terminal that has gone away can't be asked; that is
        logged."""
        self._clear_clipboard_at = 0.0
        try:
            self._write("\x1b]52;c;\a")
        except OSError as error:
            self.broker.audit.event("error", where="console", detail=f"could not clear the clipboard: {error}")

    def _lock_on_time(self) -> None:
        if not self.unlocked_until:
            return
        remaining = self.unlocked_until - self.clock()
        if remaining <= 0:
            self.lock("Changes locked again after an hour; the next one asks for your passphrase")
            self.tell("Changes are locked again. The next one asks for your passphrase.", "warn")
        elif remaining <= LOCK_WARNING_SECONDS and not self._lock_warned:
            self._lock_warned = True
            self.tell(f"Changes lock again in {math.ceil(remaining / 60)} min; the next change after that asks for your passphrase.", "warn")

    def _failed(self, message: str) -> None:
        detail = traceback.format_exc(limit=5)
        if detail != self._last_failure:
            self._last_failure = detail
            self.broker.audit.event("error", where="console", detail=detail)
        self.tell(message, "error")

    def frame(self, columns: int, rows: int) -> list[str]:
        try:
            lines = self._compose(columns, rows)
        except Exception:
            self._failed("This screen could not be drawn; the broker keeps running. Esc goes back.")
            lines = [styled("envh console", ACCENT), [], mark("error", "This screen could not be drawn; the broker keeps running. Esc goes back.")]
        return [render(line, columns, self.color) for line in lines]

    def _compose(self, columns: int, rows: int) -> list[Line]:
        if columns < MIN_COLUMNS or rows < MIN_ROWS:
            lines = [styled("envh console", ACCENT), styled(f"Make this window bigger: at least {MIN_COLUMNS} by {MIN_ROWS}.")]
            return lines + ([mark("warn", "A request is waiting.")] if self.card or self.waiting else [])
        area = rows - 4
        overlay = self.card or (self.dialogs[-1] if self.dialogs else None)
        overlay_lines = overlay.body(columns, area - (1 if self.card else 3))[-area:] if overlay else []
        room = area - len(overlay_lines)
        view_lines = self.views[-1].body(columns, room)[:room]
        body = view_lines + [[] for _ in range(room - len(view_lines))] + overlay_lines
        return [self._header(columns), [], *body, self._status(), self._hints()]

    def _header(self, columns: int) -> Line:
        crumbs = [view.title() for view in self.views[1:] if view.title()]
        left = [Span("envh console", ACCENT), *(Span(f" › {crumb}", BOLD) for crumb in crumbs)]
        broker = self.broker
        sessions = len(broker.state.live_sessions())
        counts = f"{plural(len(broker.vault.names()), 'key')} · {plural(len(broker.config.presets), 'preset')}" + (f" · {plural(sessions, 'live session')}" if sessions else "")
        waiting = len(self.waiting) + (1 if self.card is not None and not self.card.withdrawn else 0)
        right = [Span(counts, DIM), *([Span(f" · {waiting} waiting", YELLOW)] if waiting else [])]
        if self.unlocked:
            remaining = self.unlocked_until - self.clock()
            right.append(Span(f" · unlocked {math.ceil(remaining / 60)} min", YELLOW if remaining <= LOCK_WARNING_SECONDS else DIM))
        gap = columns - line_width(left) - line_width(right)
        return left + [Span(" " * gap), *right] if gap >= 2 else left

    def _status(self) -> Line:
        paused = self.paused_until - self.clock()
        if paused > 0:
            line = mark("error", f"Wrong passphrase. Typing is paused for {math.ceil(paused)} s.")
        elif self.message is not None:
            line = mark(*self.message)
        elif self.waiting and self.card is None:
            line = mark("warn", f"{plural(len(self.waiting), 'request')} waiting. It opens once you finish here (Enter or Esc).", YELLOW)
        else:
            line = self.views[-1].status() or []
        return [Span("  "), *line] if line else []

    def _hints(self) -> Line:
        target = self.card or (self.dialogs[-1] if self.dialogs else self.views[-1])
        return [Span("  " + target.hints(), DIM)]

    def redraw(self) -> None:
        if self.terminal is not None and self.display is not None:
            size = self.terminal.size()
            self.display.draw(self.frame(*size), size)

    def suspend(self, action: Callable[[], None]) -> None:
        """Run action with the terminal handed back as it was, for an editor; the console redraws afterwards."""
        if self.terminal is None:
            action()
            return
        with self.terminal.suspended():
            action()
        if self.display is not None:
            self.display.invalidate()

    async def run(self, reader: asyncio.StreamReader, terminal: Terminal) -> None:
        """Draw and read keys until stop(). Whatever ends the loop, the terminal is put back and the console stops."""
        self.terminal = terminal
        self.display = Display(terminal.write)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        keys = KeyReader()
        reading: asyncio.Future[bytes] | None = None
        try:
            with terminal.full_screen():
                while not self.quit_requested.is_set():
                    self.redraw()
                    if reading is None:
                        reading = asyncio.ensure_future(reader.read(4096))
                    waking = asyncio.ensure_future(self.wake.wait())
                    timeout = ESCAPE_WAIT_SECONDS if keys.waiting else REFRESH_SECONDS
                    done, _ = await asyncio.wait({reading, waking}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                    waking.cancel()
                    self.wake.clear()
                    pressed: list[Key] = []
                    if reading in done:
                        data = reading.result()
                        reading = None
                        if not data:
                            break
                        pressed = keys.feed(decoder.decode(data))
                    elif not done and keys.waiting:
                        pressed = keys.flush()
                    for key in pressed:
                        self.press(key)
                    self.tick()
        finally:
            if reading is not None:
                reading.cancel()
            if self._clear_clipboard_at:
                self._clear_clipboard()
            self.terminal = None
            self.stop()
