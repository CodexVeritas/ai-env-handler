"""Headless envh broker for the arena. Run as the service user with the passphrase in env.

Parametric version of the 2026-10-05 CTF broker: reads the data dir, socket path, and
passphrase from the environment. This is the SAME broker + control-socket code path the
production console uses (see src/envh/server/serve.py), minus the full-screen TUI and WITHOUT
any approver: no request is ever approved here except keys whose policy is `auto` (which the
broker grants with no console, by design -- the positive control relies on this). An agent
that extracts any other secret value therefore did so without a human approval, i.e. via a
genuine bug, which is exactly what the red team probes.

The production `envh serve` refuses to start on a host with legacy_tiocsti=1 / no Yama (this
container); those guards protect the *console terminal*, which agents cannot reach anyway. The
agent-facing surface (vault, config, state, socket protocol, broker decisions) is identical.

Environment:
  ENVH_ARENA_DATA     data dir holding the vault/config/presets (required)
  ENVH_ARENA_SOCKET   Unix socket path to listen on (required; keep < 100 bytes)
  ENVH_ARENA_PASS     vault passphrase (required)
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from envh.core.audit import AUDIT_FILE, Audit
from envh.core.broker import Broker
from envh.core.config import load_config
from envh.core.state import StateTable
from envh.core.vault import VAULT_FILE, Vault
from envh.server.control import ControlServer

SWEEP_INTERVAL_SECONDS = 15


def local_now() -> datetime:
    return datetime.now().astimezone()


async def main() -> int:
    data = Path(os.environ["ENVH_ARENA_DATA"])
    socket_path = Path(os.environ["ENVH_ARENA_SOCKET"])
    passphrase = os.environ.get("ENVH_ARENA_PASS")
    if not passphrase:
        print("ENVH_ARENA_PASS not set", file=sys.stderr)
        return 2
    vault = Vault.open(data / VAULT_FILE, passphrase)
    config = load_config(data, set(vault.names()))
    state = StateTable(local_now)
    audit = Audit(data / AUDIT_FILE, lambda event: None, local_now)
    broker = Broker(data, config, vault, state, audit)
    control = ControlServer(broker, socket_path)
    await control.start()
    audit.event("arena_broker_start", pid=os.getpid(), socket=str(socket_path), keys=len(vault.names()))
    print(f"arena broker listening on {socket_path} with {len(vault.names())} keys; NO approver (auto keys release by design).", flush=True)

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            for request in state.sweep_abandoned():
                audit.event("abandoned", id=request.id)
            state.prune_history()

    sweep_task = asyncio.create_task(sweeper())
    try:
        await asyncio.Event().wait()  # run forever
    finally:
        sweep_task.cancel()
        await control.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
