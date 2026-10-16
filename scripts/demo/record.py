"""Record the envh demo: a development broker's console and an agent's shell, driven through two ptys, with fake keys.

Writes a JSON recording of both terminals that render.py turns into the demo video. From the repo root:

    uv run python scripts/demo/record.py build/demo/recording.json
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import getpass
import json
import os
import pty
import random
import re
import secrets
import signal
import string
import struct
import sys
import tempfile
import termios
import threading
import time
from datetime import timedelta
from pathlib import Path

from envh.core.config import CONFIG_FILE, PRESETS_FILE, Defaults, SecretEntry, Settings, render_config
from envh.core.vault import VAULT_FILE, Vault, write_private_file
from envh.server.init_cmd import PHRASE_FILE

COLUMNS, ROWS = 80, 30
WAIT_TIMEOUT_SECONDS = 20
CONSOLE_PHRASE = "amber basil cedar"
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][A-Z0-9]|[=>78])")
DOWN = "\x1b[B"
ENTER = "\r"
CTRL_C = "\x03"

PRESETS_TEXT = """presets:
  news-bot:
    env:
      OPENAI_API_KEY: NEWS_BOT_OPENAI_API_KEY
      ASKNEWS_API_KEY: NEWS_BOT_ASKNEWS_API_KEY
"""
ENV_TEXT = """# OPENAI_API_KEY -> envh secret NEWS_BOT_OPENAI_API_KEY (presets: news-bot)
# ASKNEWS_API_KEY -> envh secret NEWS_BOT_ASKNEWS_API_KEY (presets: news-bot)
LOG_LEVEL=info
"""
FETCH_SCRIPT = """import os
import time

print("fetch.py: got OPENAI_API_KEY and ASKNEWS_API_KEY" if os.environ.get("OPENAI_API_KEY") else "fetch.py: no keys")
time.sleep(0.6)
print("fetch.py: summarized 14 articles into digest.md")
"""
CLEANUP_SCRIPT = 'print("cleanup.py: connected to the database")\n'
# The request card shows the client's path twice, so a short one keeps the card readable.
SHORT_PATH_CLIENT = """#!/usr/bin/env python3
import sys

from envh.cli import main

sys.exit(main())
"""


class Clock:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.marks: dict[str, float] = {}

    def now(self) -> float:
        return round(time.monotonic() - self.started, 4)

    def log(self, text: str) -> None:
        print(f"[{self.now():6.2f}s] {text}", flush=True)

    def mark(self, name: str) -> None:
        self.marks[name] = self.now()


class Pane:
    """A program on its own pty of COLUMNS x ROWS, with everything it prints kept with the time it arrived."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], cwd: Path, clock: Clock) -> None:
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
            os.execvpe(argv[0], argv, env)
        self.name = name
        self.pid = pid
        self.descriptor = descriptor
        self.clock = clock
        self.events: list[tuple[float, str]] = []
        self.raw = ""
        self.lock = threading.Lock()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.descriptor, 65536)
            except OSError:
                return
            if not data:
                return
            text = self.decoder.decode(data)
            with self.lock:
                self.events.append((self.clock.now(), text))
                self.raw += text

    def checkpoint(self) -> int:
        with self.lock:
            return len(self.raw)

    def wait_for(self, needle: str, since: int = 0) -> str:
        """The pane's text since the checkpoint, without escape codes, once it contains needle."""
        deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self.lock:
                plain = ANSI.sub("", self.raw[since:])
            if needle in plain:
                return plain
            time.sleep(0.05)
        with self.lock:
            tail = ANSI.sub("", self.raw[-1500:])
        raise TimeoutError(f"{self.name}: {needle!r} did not appear within {WAIT_TIMEOUT_SECONDS}s. Last output:\n{tail}")

    def send(self, text: str) -> None:
        os.write(self.descriptor, text.encode())

    def type(self, text: str, seconds_per_char: float = 0.035) -> None:
        for char in text:
            self.send(char)
            time.sleep(seconds_per_char * random.uniform(0.6, 1.4))

    def stop(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(self.pid, 0)


def fake_value(prefix: str, length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(length))


def seed(data_dir: Path, project_dir: Path) -> str:
    """A vault of fake keys with a news-bot preset, and a project whose .env was already imported. Returns the passphrase."""
    data_dir.mkdir(mode=0o700)
    project_dir.mkdir()
    passphrase = fake_value("demo-", 16)
    vault = Vault.create(data_dir / VAULT_FILE, passphrase)
    vault.set("NEWS_BOT_OPENAI_API_KEY", fake_value("sk-proj-DEMO", 60))
    vault.set("NEWS_BOT_ASKNEWS_API_KEY", fake_value("DEMO", 40))
    vault.set("PROD_DATABASE_URL", f"postgresql://app:{fake_value('DEMO', 20)}@db.internal:5432/prod")
    vault.save()
    settings = Settings(
        users=(getpass.getuser(),),
        notify=False,
        defaults=Defaults(approval="session", max_session=timedelta(hours=1)),
        secrets={
            "NEWS_BOT_OPENAI_API_KEY": SecretEntry(description="Team OpenAI account"),
            "NEWS_BOT_ASKNEWS_API_KEY": SecretEntry(description="Team AskNews account"),
            "PROD_DATABASE_URL": SecretEntry(approval="per-run", description="Production database, read-write"),
        },
    )
    write_private_file(data_dir / CONFIG_FILE, render_config(settings).encode())
    write_private_file(data_dir / PRESETS_FILE, PRESETS_TEXT.encode())
    write_private_file(data_dir / PHRASE_FILE, f"{CONSOLE_PHRASE}\n".encode())
    (project_dir / ".env").write_text(ENV_TEXT)
    (project_dir / "fetch.py").write_text(FETCH_SCRIPT)
    (project_dir / "cleanup.py").write_text(CLEANUP_SCRIPT)
    return passphrase


def run_command(agent: Pane, command: str, clock: Clock, mark: str) -> None:
    clock.mark(mark)
    agent.type(command)
    time.sleep(0.3)
    agent.send(ENTER)


def perform(console: Pane, agent: Pane, passphrase: str, clock: Clock) -> None:
    """The demo itself. The marks name the moments render.py hangs captions on."""
    clock.mark("start")
    time.sleep(1.2)
    since = agent.checkpoint()
    run_command(agent, "cat .env", clock, "env_typing")
    agent.wait_for("LOG_LEVEL=info", since)
    time.sleep(4.5)

    clock.log("The agent asks for a session")
    since = console.checkpoint()
    run_command(agent, 'envh session start news-bot --minutes 60 --reason "daily news digest"', clock, "session_typing")
    console.wait_for("Session request #", since)
    clock.mark("card1")
    time.sleep(5.0)
    clock.mark("typing_passphrase")
    since = agent.checkpoint()
    console.type(passphrase, seconds_per_char=0.07)
    time.sleep(0.4)
    console.send(ENTER)
    output = agent.wait_for("expires", since)
    clock.mark("approved")
    session_id = re.search(r"session ([0-9a-f]+) approved", output).group(1)
    time.sleep(2.5)

    clock.log("The agent runs a script inside the session")
    since = agent.checkpoint()
    run_command(agent, f'envh run --session {session_id} --reason "fetch news" -- python3 fetch.py', clock, "run_typing")
    agent.wait_for("digest.md", since)
    clock.mark("ran")
    time.sleep(3.0)

    clock.log("The agent asks for a key it should not need")
    since = console.checkpoint()
    run_command(agent, "envh run --with DATABASE_URL=PROD_DATABASE_URL -- python3 cleanup.py", clock, "deny_typing")
    console.wait_for("Run request #", since)
    clock.mark("card2")
    time.sleep(5.0)
    since = agent.checkpoint()
    console.send(DOWN)
    clock.mark("deny_chosen")
    time.sleep(1.0)
    console.send(ENTER)
    agent.wait_for("denied", since)
    clock.mark("denied")
    time.sleep(3.0)
    clock.mark("activity")
    time.sleep(4.0)
    clock.mark("end")


def record(output: Path) -> None:
    random.seed(7)
    clock = Clock()
    venv_bin = Path(sys.executable).parent
    with tempfile.TemporaryDirectory(prefix="envh-demo-", dir=os.environ.get("XDG_RUNTIME_DIR")) as work:
        data_dir = Path(work) / "data"
        project_dir = Path(work) / "news-bot"
        bin_dir = Path(work) / "bin"
        socket = Path(work) / "ctl.sock"
        passphrase = seed(data_dir, project_dir)
        bin_dir.mkdir()
        (bin_dir / "envh").write_text(SHORT_PATH_CLIENT)
        (bin_dir / "envh").chmod(0o755)
        env = {
            "PATH": f"{bin_dir}:{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ["HOME"],
            "USER": getpass.getuser(),
            "LOGNAME": getpass.getuser(),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "TZ": "UTC",
            "ENVH_SOCKET": str(socket),
        }
        clock.log("Starting a development broker with fake keys")
        console = Pane("console", [str(bin_dir / "envh"), "serve", "--data-dir", str(data_dir), "--socket", str(socket)], env, data_dir, clock)
        agent: Pane | None = None
        try:
            console.wait_for("Vault passphrase:")
            console.send(passphrase + ENTER)
            console.wait_for("Console ready")
            clock.log("Console ready; starting the agent's shell")
            agent_env = {**env, "PS1": r"\[\e[1;32m\]$\[\e[0m\] ", "HISTFILE": "/dev/null"}
            agent = Pane("agent", ["bash", "--norc", "--noprofile", "-i"], agent_env, project_dir, clock)
            agent.wait_for("$ ")
            time.sleep(0.8)
            perform(console, agent, passphrase, clock)
        finally:
            clock.log("Stopping the console and the shell")
            console.send(CTRL_C)
            time.sleep(0.3)
            console.send(CTRL_C)
            time.sleep(1.5)
            console.stop()
            if agent is not None:
                agent.stop()
    missing = {"start", "end"} - set(clock.marks)
    if missing:
        raise RuntimeError(f"the recording stopped early; missing marks: {', '.join(sorted(missing))}")
    output.parent.mkdir(parents=True, exist_ok=True)
    panes = {"console": console.events, "agent": agent.events}
    output.write_text(json.dumps({"columns": COLUMNS, "rows": ROWS, "marks": clock.marks, "panes": panes}))
    clock.log(f"Wrote {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the envh demo's two terminals")
    parser.add_argument("output", type=Path, nargs="?", default=Path("build/demo/recording.json"))
    record(parser.parse_args().output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
