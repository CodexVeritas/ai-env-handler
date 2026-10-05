"""`envh serve`: the broker process with its console, run on the service user's own terminal."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from envh.platform import socket_path as default_socket_path
from envh.core.audit import AUDIT_FILE, Audit
from envh.core.broker import Broker
from envh.core.config import ConfigError, load_config
from envh.server.console.activity import Activity
from envh.server.console.app import ConsoleApp
from envh.server.console.summaries import plural
from envh.server.console.tui import Terminal
from envh.server.control import ControlServer
from envh.server.hardening import HardeningError, acquire_instance_lock, assert_no_tiocsti, assert_terminal_is_ours, harden_process
from envh.server.init_cmd import PHRASE_FILE, default_data_dir
from envh.core.state import StateTable
from envh.core.vault import VAULT_FILE, Vault, VaultError

SWEEP_INTERVAL_SECONDS = 15
UNLOCK_ATTEMPTS = 3
COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def say(text: str = "") -> None:
    print(text, flush=True)


def write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def ok(text: str) -> None:
    say(f"  {paint('32', '✓')} {text}")


def failed(text: str) -> str:
    return f"  {paint('31', '✗')} {text}"


def local_now() -> datetime:
    """Timezone-aware local time, so session durations stay exact across daylight-saving changes."""
    return datetime.now().astimezone()


def read_phrase(data_dir: Path) -> str:
    try:
        return (data_dir / PHRASE_FILE).read_text().strip()
    except FileNotFoundError as error:
        raise SystemExit(failed(f"{data_dir / PHRASE_FILE} is missing; rerun the setup wizard.")) from error


def unlock_vault(data_dir: Path, phrase: str) -> Vault:
    say()
    say(f"  {paint('1;36', 'envh console')}")
    say(f"  {paint('2', 'Approves the keys your scripts and agents ask for.')}")
    say()
    say(f"  Console phrase: {paint('36', phrase)}")
    say(f"  {paint('2', 'Type your vault passphrase only where this phrase is shown.')}")
    for attempt in range(UNLOCK_ATTEMPTS):
        say()
        passphrase = getpass.getpass("  Vault passphrase: ")
        write("  Unlocking the vault…")
        try:
            vault = Vault.open(data_dir / VAULT_FILE, passphrase)
        except VaultError as error:
            write("\r\033[K")
            say(failed(f"{str(error)[:1].upper()}{str(error)[1:]}." + (" Try again." if attempt < UNLOCK_ATTEMPTS - 1 else "")))
            continue
        write("\r\033[K")
        ok(f"Vault unlocked · {plural(len(vault.names()), 'key')}")
        return vault
    raise SystemExit(failed("Couldn't unlock the vault."))


async def run_broker(data_dir: Path, socket_path: Path) -> None:
    loop = asyncio.get_running_loop()
    phrase = read_phrase(data_dir)
    vault = unlock_vault(data_dir, phrase)
    try:
        config = load_config(data_dir, set(vault.names()))
    except ConfigError as error:
        raise SystemExit(failed(f"The policy files have a problem: {error}"))
    ok(f"Policy loaded · {plural(len(config.presets), 'preset')}")
    state = StateTable(local_now)
    activity = Activity(local_now)
    audit = Audit(data_dir / AUDIT_FILE, activity.add, local_now)
    broker = Broker(data_dir, config, vault, state, audit)
    control = ControlServer(broker, socket_path)
    try:
        await control.start()
    except OSError as error:
        raise SystemExit(failed(f"Can't listen for requests: {error}")) from error
    ok("Listening for requests")

    tty_fd = os.open(os.ttyname(sys.stdin.fileno()), os.O_RDONLY | os.O_NOCTTY)
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(tty_fd, "rb", buffering=0))
    app = ConsoleApp(broker, phrase, activity, write=write, color=COLOR)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signum, app.stop)
    loop.add_signal_handler(signal.SIGWINCH, app.wake.set)
    audit.event("broker_start", pid=os.getpid(), socket=str(socket_path))
    activity.note("ok", f"Console ready · {plural(len(vault.names()), 'key')} · {plural(len(broker.config.presets), 'preset')}")

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            for request in state.sweep_abandoned():
                audit.event("abandoned", id=request.id)
            state.prune_history()

    console_task = asyncio.create_task(app.run(reader, Terminal(tty_fd, write)))
    sweeper_task = asyncio.create_task(sweeper())
    await app.quit_requested.wait()
    sweeper_task.cancel()
    [outcome] = await asyncio.gather(console_task, return_exceptions=True)
    ended = state.live_sessions()
    for session in ended:
        broker.end_session(session.id, by="shutdown")
    await control.close()
    audit.event("broker_stop", pid=os.getpid())
    if isinstance(outcome, Exception):
        audit.event("error", where="console", detail=repr(outcome))
        say(failed(f"The console stopped because of an error: {outcome!r}"))
    say()
    ok("Console stopped" + (f" · {plural(len(ended), 'live session')} ended" if ended else ""))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="envh serve", description="Run the broker and its console on this terminal")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--socket", type=Path, default=None)
    args = parser.parse_args(argv)
    data_dir = args.data_dir or default_data_dir()
    socket_path = args.socket or default_socket_path()
    try:
        assert_no_tiocsti()
        assert_terminal_is_ours()
        harden_process()
        lock_descriptor = acquire_instance_lock(data_dir)
    except HardeningError as error:
        print(failed(f"The console can't start: {error}"), file=sys.stderr)
        return 1
    try:
        asyncio.run(run_broker(data_dir, socket_path))
    finally:
        os.close(lock_descriptor)
    return 0
