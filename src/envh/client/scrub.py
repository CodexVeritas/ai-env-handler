"""Hides the values a run received in what its command prints, so a key shown by accident never reaches a terminal, a
log or an agent's transcript. Only the exact value is matched: an encoded or reshaped copy gets through."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import signal
import termios
import threading
import time
from types import FrameType, TracebackType

PLACEHOLDER = "[envh:{}]"
SHORTEST_HIDDEN_VALUE = 8
IDLE_FLUSH_SECONDS = 0.1
DRAIN_SECONDS = 1.0
READ_SIZE = 65536


class Scrubber:
    """Replaces each value with a placeholder naming its variable, even when the value arrives split across writes. An
    ending that could be the start of a value is held back until the next write or flush()."""

    def __init__(self, env: dict[str, str]) -> None:
        self.names: dict[bytes, str] = {}
        for name, value in sorted(env.items()):
            encoded = os.fsencode(value)
            if len(encoded) >= SHORTEST_HIDDEN_VALUE:
                self.names.setdefault(encoded, name)
        self.values = sorted(self.names, key=len, reverse=True)
        self.pattern = re.compile(b"|".join(map(re.escape, self.values))) if self.values else None
        self.first_bytes = {value[0] for value in self.values}
        self.held = b""
        self.hidden: set[str] = set()

    def feed(self, data: bytes) -> bytes:
        if self.pattern is None:
            return data
        buffer = self.held + data
        pieces: list[bytes] = []
        end = 0
        for match in self.pattern.finditer(buffer):
            name = self.names[match.group()]
            pieces += [buffer[end:match.start()], PLACEHOLDER.format(name).encode()]
            self.hidden.add(name)
            end = match.end()
        keep = len(buffer) - self.partial_value_length(buffer[end:])
        pieces.append(buffer[end:keep])
        self.held = buffer[keep:]
        return b"".join(pieces)

    def flush(self) -> bytes:
        held, self.held = self.held, b""
        return held

    def partial_value_length(self, tail: bytes) -> int:
        """Length of the longest ending of tail that some value starts with."""
        for start in range(max(0, len(tail) - len(self.values[0]) + 1), len(tail)):
            if tail[start] in self.first_bytes and any(value.startswith(tail[start:]) for value in self.values):
                return len(tail) - start
        return 0


class Channel:
    """One place the command's output goes: the command writes into child_fd, and a thread copies it on to target_fd
    through a Scrubber. A terminal target gets a pseudo-terminal, so the command still sees a terminal."""

    def __init__(self, target_fd: int, env: dict[str, str]) -> None:
        self.target_fd = target_fd
        self.scrubber = Scrubber(env)
        self.is_terminal = os.isatty(target_fd)
        if self.is_terminal:
            self.read_fd, self.child_fd = pty.openpty()
            attributes = termios.tcgetattr(self.child_fd)
            attributes[1] &= ~termios.ONLCR
            termios.tcsetattr(self.child_fd, termios.TCSANOW, attributes)
            self.copy_window_size()
        else:
            self.read_fd, self.child_fd = os.pipe()
        self.thread: threading.Thread | None = None

    def copy_window_size(self) -> None:
        fcntl.ioctl(self.read_fd, termios.TIOCSWINSZ, fcntl.ioctl(self.target_fd, termios.TIOCGWINSZ, b"\0" * 8))

    def start(self, stopping: threading.Event) -> None:
        self.thread = threading.Thread(target=self.relay, args=(stopping,), daemon=True)
        self.thread.start()

    def join(self) -> None:
        if self.thread is not None:
            self.thread.join()

    def relay(self, stopping: threading.Event) -> None:
        try:
            self.copy(stopping)
        except OSError as error:
            # Closing our end passes the failure on: the command's next write fails instead of waiting for us forever.
            # A reader that stopped early, as with `| head`, is ordinary and needs no traceback.
            self.close_read_end()
            if not isinstance(error, BrokenPipeError):
                raise

    def copy(self, stopping: threading.Event) -> None:
        deadline: float | None = None
        while True:
            if deadline is None and stopping.is_set():
                deadline = time.monotonic() + DRAIN_SECONDS
            if select.select([self.read_fd], [], [], IDLE_FLUSH_SECONDS)[0]:
                data = self.read()
                if not data:
                    break
                self.write(self.scrubber.feed(data))
                if deadline is not None and time.monotonic() > deadline:
                    break
            else:
                self.write(self.scrubber.flush())
                if deadline is not None:
                    break
        self.write(self.scrubber.flush())

    def read(self) -> bytes:
        try:
            return os.read(self.read_fd, READ_SIZE)
        except OSError as error:
            if error.errno == errno.EIO and self.is_terminal:
                return b""
            raise

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self.target_fd, view):]

    def close_child_end(self) -> None:
        if self.child_fd >= 0:
            os.close(self.child_fd)
            self.child_fd = -1

    def close_read_end(self) -> None:
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1


class ScrubbedOutput:
    """The command's stdout and stderr, each relayed through its own Channel. When both go to the same place they share
    one, which keeps their order. Leaving the block stops the relay once the output pauses or DRAIN_SECONDS pass, so a
    leftover process still holding the output cannot keep the run open."""

    def __init__(self, env: dict[str, str]) -> None:
        stdout = Channel(1, env)
        stderr = stdout if os.path.samestat(os.fstat(1), os.fstat(2)) else Channel(2, env)
        self.channels = [stdout] if stderr is stdout else [stdout, stderr]
        self.stdout, self.stderr = stdout.child_fd, stderr.child_fd
        self.terminals = [channel for channel in self.channels if channel.is_terminal]
        self.stopping = threading.Event()
        self.previous_resize_handler = signal.getsignal(signal.SIGWINCH)

    def __enter__(self) -> ScrubbedOutput:
        if self.terminals:
            signal.signal(signal.SIGWINCH, self.resize)
        for channel in self.channels:
            channel.start(self.stopping)
        return self

    def command_started(self) -> None:
        """Drops this process's copies of the command's ends, so a channel sees the end of output when the command's
        side closes."""
        for channel in self.channels:
            channel.close_child_end()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.command_started()
        self.stopping.set()
        for channel in self.channels:
            channel.join()
        if self.terminals:
            signal.signal(signal.SIGWINCH, self.previous_resize_handler)
        for channel in self.channels:
            channel.close_read_end()

    def resize(self, _signum: int, _frame: FrameType | None) -> None:
        for channel in self.terminals:
            channel.copy_window_size()

    @property
    def hidden(self) -> list[str]:
        return sorted({name for channel in self.channels for name in channel.scrubber.hidden})
