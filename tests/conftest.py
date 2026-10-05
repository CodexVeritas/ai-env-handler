from __future__ import annotations

import asyncio
import getpass
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from envh.core.audit import Audit
from envh.core.broker import Broker
from envh.core.config import load_config, render_config_template
from envh.server.control import ControlServer
from envh.core.state import Provenance, StateTable
from envh.core.vault import Vault

PASSPHRASE = "test-passphrase"
PASSPHRASE_LINE = f"{PASSPHRASE}\n".encode()


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 2, 14, 0, 0)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class Harness:
    data_dir: Path
    broker: Broker
    clock: FakeClock
    echoed: list[str] = field(default_factory=list)

    def provenance(self) -> Provenance:
        return Provenance(pid=4242, uid=1000, cmdline="envh test")


ME = getpass.getuser()


def build_broker(data_dir: Path, echoed: list[str], clock: FakeClock, config_text: str = render_config_template(ME), presets_text: str = "presets: {}\n") -> Broker:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(config_text)
    (data_dir / "presets.yaml").write_text(presets_text)
    vault = Vault(data_dir / "vault.age", PASSPHRASE, {"OPENAI_API_KEY": "sk-openai", "TEAM_OPENROUTER_KEY": "sk-or-team", "DATABASE_URL": "postgres://x"})
    config = load_config(data_dir, set(vault.names()))
    audit = Audit(data_dir / "audit.jsonl", echoed.append, clock)
    return Broker(data_dir, config, vault, StateTable(clock), audit)


@pytest.fixture(autouse=True)
def no_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Tests and the envh clients they start never reach the desktop: without a display envh doesn't notify, and with
    the session bus pointed at a missing socket, no notification or chime could get through anyway."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'no-session-bus'}")


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    clock = FakeClock()
    echoed: list[str] = []
    presets = """
presets:
  team:
    max_session: 3h
    env:
      OPENROUTER_API_KEY: TEAM_OPENROUTER_KEY
      OPENAI_API_KEY: OPENAI_API_KEY
  dbwork:
    env:
      DATABASE_URL: {secret: DATABASE_URL, approval: per-run}
      OPENAI_API_KEY: OPENAI_API_KEY
"""
    config = f"users: [{ME}, nobody]\ndefaults: {{approval: session, max_session: 2h}}\nsecrets: {{OPENAI_API_KEY: {{max_session: 1h}}}}\n"
    broker = build_broker(tmp_path / "data", echoed, clock, config, presets)
    return Harness(data_dir=tmp_path / "data", broker=broker, clock=clock, echoed=echoed)


@pytest.fixture
async def control(harness: Harness, tmp_path: Path) -> AsyncIterator[Path]:
    socket_path = tmp_path / "ctl.sock"
    server = ControlServer(harness.broker, socket_path)
    await server.start()
    try:
        yield socket_path
    finally:
        await server.close()


class Client:
    """Minimal NDJSON client over a Unix socket for tests."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    async def __aenter__(self) -> "Client":
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.socket_path))
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.writer is not None
        self.writer.close()
        await self.writer.wait_closed()

    async def send(self, **message: Any) -> None:
        assert self.writer is not None
        self.writer.write(json.dumps(message).encode() + b"\n")
        await self.writer.drain()

    async def recv(self) -> dict[str, Any]:
        assert self.reader is not None
        line = await asyncio.wait_for(self.reader.readline(), timeout=5)
        assert line, "connection closed"
        return json.loads(line)


async def approve_next(harness: Harness) -> None:
    for _ in range(50):
        pending = harness.broker.state.pending()
        if pending:
            harness.broker.approve(pending[0])
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no pending request appeared")
