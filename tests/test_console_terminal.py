"""The console's real loop on a pseudo-terminal: what the terminal receives, and how it is left."""

import asyncio
import fcntl
import os
import struct
import termios
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from envh.server.console.activity import Activity
from envh.server.console.app import ConsoleApp
from envh.server.console.tui import ENTER_FULL_SCREEN, LEAVE_FULL_SCREEN, Terminal
from tests.conftest import PASSPHRASE, Harness


@dataclass
class Session:
    app: ConsoleApp
    master: int
    output: bytearray
    task: asyncio.Task[None]

    async def wait_for(self, text: str) -> int:
        for _ in range(200):
            found = self.output.decode(errors="replace").find(text)
            if found >= 0:
                return found
            await asyncio.sleep(0.01)
        raise AssertionError(f"{text!r} never appeared on the terminal")

    def type(self, keys: bytes) -> None:
        os.write(self.master, keys)

    @property
    def text(self) -> str:
        return self.output.decode(errors="replace")


@asynccontextmanager
async def console_on_a_terminal(harness: Harness) -> AsyncIterator[Session]:
    """The console running on a fresh pseudo-terminal, which must have its modes back once the console stops."""
    loop = asyncio.get_running_loop()
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    modes_before = termios.tcgetattr(slave)
    read_fd = os.open(os.ttyname(slave), os.O_RDONLY | os.O_NOCTTY)
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0))
    output = bytearray()
    loop.add_reader(master, lambda: output.extend(os.read(master, 65536)))

    def write(text: str) -> None:
        os.write(slave, text.encode())

    app = ConsoleApp(harness.broker, "amber basil cedar", Activity(harness.clock), write=write)
    session = Session(app, master, output, asyncio.create_task(app.run(reader, Terminal(read_fd, write))))
    try:
        await session.wait_for("Type / for commands")
        yield session
        assert termios.tcgetattr(slave) == modes_before
    finally:
        loop.remove_reader(master)
        transport.close()
        os.close(master)
        os.close(slave)


async def test_the_console_runs_full_screen_and_puts_the_terminal_back(harness: Harness) -> None:
    async with console_on_a_terminal(harness) as session:
        assert session.output.startswith(ENTER_FULL_SCREEN.encode())
        session.type(b"/keys\r")
        await session.wait_for("Keys")
        request = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, [], "e2e", ("python", "x.py"), harness.provenance())
        await session.wait_for(f"Run request #{request.id}")
        session.type(PASSPHRASE.encode() + b"\r")
        await asyncio.wait_for(asyncio.shield(request.decision), 5)
        assert request.decision.result().outcome == "approved"
        session.type(b"\x03\x03\x03")
        await asyncio.wait_for(session.task, 5)
        await asyncio.sleep(0.05)
    assert session.text.rindex(LEAVE_FULL_SCREEN) > session.text.rindex(ENTER_FULL_SCREEN)
    assert PASSPHRASE not in session.text
    assert session.app.quit_requested.is_set()
