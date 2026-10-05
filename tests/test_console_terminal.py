"""The console's real loop on a pseudo-terminal: what the terminal receives, and how it is left."""

import asyncio
import fcntl
import os
import struct
import termios

from envh.server.console.activity import Activity
from envh.server.console.app import ConsoleApp
from envh.server.console.tui import ENTER_FULL_SCREEN, LEAVE_FULL_SCREEN, Terminal
from tests.conftest import PASSPHRASE, Harness


async def wait_for(output: bytearray, text: str, after: int = 0) -> int:
    for _ in range(200):
        found = output.decode(errors="replace").find(text, after)
        if found >= 0:
            return found
        await asyncio.sleep(0.01)
    raise AssertionError(f"{text!r} never appeared on the terminal")


async def test_the_console_runs_full_screen_and_puts_the_terminal_back(harness: Harness) -> None:
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
    task = asyncio.create_task(app.run(reader, Terminal(read_fd, write)))
    try:
        await wait_for(output, "Type / for commands")
        assert output.startswith(ENTER_FULL_SCREEN.encode())
        os.write(master, b"/keys\r")
        await wait_for(output, "Keys")
        request = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, [], "e2e", ("python", "x.py"), harness.provenance())
        await wait_for(output, "Run request #1")
        os.write(master, PASSPHRASE.encode() + b"\r")
        await asyncio.wait_for(asyncio.shield(request.decision), 5)
        assert request.decision.result().outcome == "approved"
        os.write(master, b"\x03\x03\x03")
        await asyncio.wait_for(task, 5)
        await asyncio.sleep(0.05)
        assert termios.tcgetattr(slave) == modes_before
    finally:
        loop.remove_reader(master)
        transport.close()
        os.close(master)
        os.close(slave)
    text = output.decode(errors="replace")
    assert text.rindex(LEAVE_FULL_SCREEN) > text.rindex(ENTER_FULL_SCREEN)
    assert PASSPHRASE not in text
    assert app.quit_requested.is_set()
