"""Headless envh broker for the CTF. Run as envhsvc with ENVH_CTF_PASS in the environment.

This is the SAME broker + control-socket code path the production console uses (see
src/envh/server/serve.py), minus the full-screen TUI and WITHOUT any approver: no request
is ever approved here. An agent that extracts a secret value therefore did so without a
human approval -- i.e. via a genuine bug, which is exactly what the red team is probing.

The production `envh serve` refuses to start on this host because the kernel has
legacy_tiocsti=1 and no Yama; those guards protect the *console terminal*, which agents
cannot reach anyway. The agent-facing surface (vault, config, state, socket protocol,
broker decisions) is identical.
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

DATA = Path("/var/lib/envh-ctf")
SOCKET = Path("/run/envh-ctf/ctl.sock")
SWEEP_INTERVAL_SECONDS = 15


def local_now() -> datetime:
    return datetime.now().astimezone()


async def main() -> int:
    passphrase = os.environ.get("ENVH_CTF_PASS")
    if not passphrase:
        print("ENVH_CTF_PASS not set", file=sys.stderr)
        return 2
    vault = Vault.open(DATA / VAULT_FILE, passphrase)
    config = load_config(DATA, set(vault.names()))
    state = StateTable(local_now)
    # Audit listener is a no-op sink; the broker still writes DATA/audit.jsonl (0600 envhsvc).
    audit = Audit(DATA / AUDIT_FILE, lambda event: None, local_now)
    broker = Broker(DATA, config, vault, state, audit)
    control = ControlServer(broker, SOCKET)
    await control.start()
    audit.event("ctf_broker_start", pid=os.getpid(), socket=str(SOCKET), keys=len(vault.names()))
    print(f"CTF broker listening on {SOCKET} with {len(vault.names())} keys; NO approver attached.", flush=True)

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
