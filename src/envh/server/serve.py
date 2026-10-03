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
from envh.server.console import Console
from envh.server.control import ControlServer
from envh.server.hardening import HardeningError, acquire_instance_lock, assert_no_tiocsti, assert_terminal_is_ours, harden_process
from envh.server.init_cmd import PHRASE_FILE, default_data_dir
from envh.core.state import StateTable
from envh.core.vault import VAULT_FILE, Vault, VaultError

SWEEP_INTERVAL_SECONDS = 15


def say(text: str) -> None:
    print(text, flush=True)


def unlock_vault(data_dir: Path) -> Vault:
    phrase_path = data_dir / PHRASE_FILE
    if phrase_path.exists():
        say(f"console phrase: {phrase_path.read_text().strip()}")
    for attempt in range(3):
        try:
            return Vault.open(data_dir / VAULT_FILE, getpass.getpass("vault passphrase: "))
        except VaultError as error:
            say(f"{error}" + (" (try again)" if attempt < 2 else ""))
    raise SystemExit("could not unlock the vault")


async def run_broker(data_dir: Path, socket_path: Path) -> None:
    loop = asyncio.get_running_loop()
    vault = unlock_vault(data_dir)
    say(f"vault unlocked: {len(vault.names())} secrets")
    try:
        config = load_config(data_dir, set(vault.names()))
    except ConfigError as error:
        raise SystemExit(f"config error: {error}")
    say(f"config loaded: {len(config.secret_policies)} secret policies, {len(config.presets)} presets")
    state = StateTable(datetime.now)
    audit = Audit(data_dir / AUDIT_FILE, say, datetime.now)
    broker = Broker(data_dir, config, vault, state, audit)
    control = ControlServer(broker, socket_path)
    await control.start()
    say(f"control socket listening at {socket_path}")

    tty_fd = os.open(os.ttyname(sys.stdin.fileno()), os.O_RDONLY | os.O_NOCTTY)
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(tty_fd, "rb", buffering=0))
    console = Console(broker, reader, say, tty_fd)
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signum, console.quit_requested.set)
    audit.event("broker_start", pid=os.getpid(), socket=str(socket_path))

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            for request in state.sweep_abandoned():
                audit.event("abandoned", id=request.id)
            state.prune_history()

    console_task = asyncio.create_task(console.run())
    sweeper_task = asyncio.create_task(sweeper())
    await console.quit_requested.wait()
    console_task.cancel()
    sweeper_task.cancel()
    for session in state.live_sessions():
        broker.end_session(session.id, by="shutdown")
    await control.close()
    audit.event("broker_stop", pid=os.getpid())


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
        print(f"refusing to start: {error}", file=sys.stderr)
        return 1
    say(f"envh broker starting as uid {os.geteuid()}; data dir {data_dir}")
    try:
        asyncio.run(run_broker(data_dir, socket_path))
    finally:
        os.close(lock_descriptor)
    return 0
