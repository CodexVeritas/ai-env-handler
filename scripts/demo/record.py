"""Record the envh demo: a development broker's console and a shell, driven through two ptys, with fake keys.

The shell shows a plaintext .env, the console starts, `envh import` moves the keys into the vault, and an agent's
session, run and denied request follow. Writes a JSON recording of both terminals, with named marks between the
steps, that render.py turns into the demo video. From the repo root:

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
import shutil
import signal
import string
import struct
import sys
import tempfile
import termios
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from envh.core.config import CONFIG_FILE, PRESETS_FILE, PRESETS_TEMPLATE, Defaults, SecretEntry, Settings, render_config
from envh.core.vault import VAULT_FILE, Vault, write_private_file
from envh.server.init_cmd import PHRASE_FILE

SHELL_SIZE = (100, 40)
CONSOLE_SIZE = (80, 40)
WAIT_TIMEOUT_SECONDS = 20
CONSOLE_PHRASE = "amber basil cedar"
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][A-Z0-9]|[=>78])")
DOWN = "\x1b[B"
ENTER = "\r"
CTRL_C = "\x03"
CLEAR_SCREEN = "\x0c"

FETCH_SCRIPT = """import os
import time

print("fetch.py: got OPENAI_API_KEY and ASKNEWS_API_KEY" if os.environ.get("OPENAI_API_KEY") else "fetch.py: no keys")
time.sleep(0.6)
print("fetch.py: summarized 14 articles into digest.md")
"""
RATES_SCRIPT = """import os

print("rates.py: got FRED_API_KEY" if os.environ.get("FRED_API_KEY") else "rates.py: no key")
print("rates.py: charted 10-year Treasury yields into rates.png")
"""
CLEANUP_SCRIPT = 'print("cleanup.py: connected to the database")\n'
# The request card shows the client's path twice, so a short one keeps the card readable.
SHORT_PATH_CLIENT = """#!/usr/bin/env python3
import sys

from envh.cli import main

sys.exit(main())
"""
# Each prompt of the import wizard, with the mark set when it appears, for the steps the video stops on.
IMPORT_PROMPTS = {"Import which?": None, "Flip any of these": "import_classify", "Rename any?": None, "Drop any preset?": "import_presets", "Rename any preset?": None}


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
    """A program on its own pty, with everything it prints kept with the time it arrived."""

    def __init__(self, name: str, size: tuple[int, int], argv: list[str], env: dict[str, str], cwd: Path, clock: Clock) -> None:
        pid, descriptor = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
            os.execvpe(argv[0], argv, env)
        self.name = name
        self.size = size
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

    def type(self, text: str, seconds_per_char: float = 0.05) -> None:
        for char in text:
            self.send(char)
            time.sleep(seconds_per_char * random.uniform(0.6, 1.4))

    def run(self, command: str, typed_mark: str | None = None) -> int:
        """Type a command and press Enter, setting typed_mark in between; returns the checkpoint from before it."""
        since = self.checkpoint()
        self.type(command)
        time.sleep(0.4)
        if typed_mark:
            self.clock.mark(typed_mark)
            time.sleep(0.6)
        self.send(ENTER)
        return since

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
    """A vault holding a key marked auto and another project's key, and a news-bot project with its keys still in .env.
    Returns the passphrase."""
    data_dir.mkdir(mode=0o700)
    project_dir.mkdir()
    passphrase = fake_value("demo-", 16)
    vault = Vault.create(data_dir / VAULT_FILE, passphrase)
    vault.set("PROD_DATABASE_URL", f"postgresql://app:{fake_value('DEMO', 20)}@db.internal:5432/prod")
    vault.set("FRED_API_KEY", fake_value("demo", 28))
    vault.save()
    settings = Settings(
        users=(getpass.getuser(),),
        notify=False,
        defaults=Defaults(approval="session", max_session=timedelta(hours=1)),
        secrets={
            "PROD_DATABASE_URL": SecretEntry(approval="per-run", description="Production database, read-write"),
            "FRED_API_KEY": SecretEntry(approval="auto", description="Free economic data, read-only"),
        },
    )
    write_private_file(data_dir / CONFIG_FILE, render_config(settings).encode())
    write_private_file(data_dir / PRESETS_FILE, PRESETS_TEMPLATE.encode())
    write_private_file(data_dir / PHRASE_FILE, f"{CONSOLE_PHRASE}\n".encode())
    env_text = f"OPENAI_API_KEY={fake_value('sk-proj-DEMO', 48)}\nASKNEWS_API_KEY={fake_value('DEMO', 36)}\nLOG_LEVEL=info\n"
    (project_dir / ".env").write_text(env_text)
    (project_dir / "fetch.py").write_text(FETCH_SCRIPT)
    (project_dir / "rates.py").write_text(RATES_SCRIPT)
    (project_dir / "cleanup.py").write_text(CLEANUP_SCRIPT)
    return passphrase


def show_problem(shell: Pane, clock: Clock) -> None:
    clock.mark("problem")
    time.sleep(0.6)
    shell.wait_for("LOG_LEVEL=info", shell.run("cat .env"))
    clock.mark("problem_shown")
    time.sleep(0.6)


def start_console(launch: Callable[[], Pane], passphrase: str, clock: Clock) -> Pane:
    clock.mark("console_start")
    console = launch()
    console.wait_for("Vault passphrase:")
    time.sleep(0.4)
    clock.mark("console_phrase")
    time.sleep(0.6)
    console.type(passphrase, seconds_per_char=0.07)
    time.sleep(0.4)
    console.send(ENTER)
    console.wait_for("Console ready")
    time.sleep(0.6)
    clock.mark("console_ready")
    time.sleep(0.6)
    return console


def import_env(shell: Pane, console: Pane, passphrase: str, clock: Clock) -> None:
    clock.log("Importing the .env file")
    clock.mark("import")
    shell.send(CLEAR_SCREEN)
    time.sleep(0.5)
    since = shell.run("envh import .")
    for prompt, mark in IMPORT_PROMPTS.items():
        shell.wait_for(prompt, since)
        if mark:
            clock.mark(mark)
        time.sleep(1.0)
        since = shell.checkpoint()
        shell.send(ENTER)
    shell.wait_for("as shown? [Y/n]", since)
    time.sleep(1.0)
    since = shell.checkpoint()
    shell.send(ENTER)
    shell.wait_for("Proceed?", since)
    time.sleep(0.8)
    since = console.checkpoint()
    shell.run("y")
    console.wait_for("Import request #", since)
    time.sleep(0.6)
    clock.mark("import_card")
    time.sleep(0.4)
    since = shell.checkpoint()
    console.type(passphrase, seconds_per_char=0.07)
    time.sleep(0.4)
    console.send(ENTER)
    shell.wait_for("rm -r", since)
    time.sleep(1.5)
    clock.mark("import_check")
    shell.send(CLEAR_SCREEN)
    time.sleep(0.5)
    shell.wait_for("LOG_LEVEL=info", shell.run("cat .env"))
    clock.mark("import_checked")
    time.sleep(0.6)


def agent_session(agent: Pane, console: Pane, passphrase: str, clock: Clock) -> None:
    clock.log("The agent asks for a session")
    clock.mark("ask")
    agent.send(CLEAR_SCREEN)
    time.sleep(0.5)
    since = console.checkpoint()
    agent.run('envh session start news-bot --minutes 60 --reason "daily news digest"', typed_mark="ask_typed")
    console.wait_for("Session request #", since)
    time.sleep(0.6)
    clock.mark("ask_card")
    time.sleep(0.4)

    clock.mark("approve")
    time.sleep(0.4)
    since = agent.checkpoint()
    console.type(passphrase, seconds_per_char=0.07)
    time.sleep(0.4)
    console.send(ENTER)
    output = agent.wait_for("expires", since)
    time.sleep(0.6)
    clock.mark("approved")
    session_id = re.search(r"session ([0-9a-f]+) approved", output).group(1)
    time.sleep(0.4)

    clock.log("The agent runs a script inside the session")
    clock.mark("run")
    time.sleep(0.4)
    agent.wait_for("digest.md", agent.run(f'envh run --session {session_id} --reason "fetch news" -- python3 fetch.py'))
    time.sleep(0.6)
    clock.mark("ran")
    time.sleep(0.4)


def auto_approved_run(agent: Pane, console: Pane, clock: Clock) -> None:
    clock.log("The agent runs with a key marked auto")
    clock.mark("auto")
    time.sleep(0.4)
    since = console.checkpoint()
    agent.wait_for("rates.png", agent.run('envh run --with FRED_API_KEY --reason "chart rates" -- python3 rates.py'))
    console.wait_for("need no approval", since)
    time.sleep(0.6)
    clock.mark("auto_ran")
    time.sleep(0.4)


def denied_request(agent: Pane, console: Pane, clock: Clock) -> None:
    clock.log("The agent asks for a key it should not need")
    clock.mark("deny")
    time.sleep(0.4)
    since = console.checkpoint()
    agent.run("envh run --with DATABASE_URL=PROD_DATABASE_URL -- python3 cleanup.py")
    console.wait_for("Run request #", since)
    time.sleep(0.6)
    clock.mark("deny_card")
    time.sleep(0.4)
    since = agent.checkpoint()
    hint = console.checkpoint()
    console.send(DOWN)
    console.wait_for("Enter deny", hint)
    time.sleep(0.3)
    clock.mark("deny_chosen")
    time.sleep(0.7)
    console.send(ENTER)
    agent.wait_for("denied", since)
    time.sleep(0.6)
    clock.mark("denied")
    time.sleep(0.4)
    clock.mark("log")
    time.sleep(1.0)
    clock.mark("end")


def record(output: Path) -> None:
    random.seed(7)
    clock = Clock()
    venv_bin = Path(sys.executable).parent
    work = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "envh-demo"
    if work.exists():
        raise SystemExit(f"{work} exists, maybe from a recording that is still running; remove it to record again")
    work.mkdir(mode=0o700)
    panes: dict[str, Pane] = {}
    try:
        data_dir, project_dir, bin_dir, socket = work / "data", work / "news-bot", work / "bin", work / "ctl.sock"
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
            "XDG_STATE_HOME": str(work / "state"),
        }

        def launch_console() -> Pane:
            panes["console"] = Pane("console", CONSOLE_SIZE, [str(bin_dir / "envh"), "serve", "--data-dir", str(data_dir), "--socket", str(socket)], env, data_dir, clock)
            return panes["console"]

        clock.log("Starting the shell, with a plaintext .env of fake keys")
        shell_env = {**env, "PS1": r"\[\e[1;32m\]$\[\e[0m\] ", "HISTFILE": "/dev/null"}
        panes["agent"] = shell = Pane("agent", SHELL_SIZE, ["bash", "--norc", "--noprofile", "-i"], shell_env, project_dir, clock)
        shell.wait_for("$ ")
        time.sleep(0.8)
        show_problem(shell, clock)
        clock.log("Starting a development broker and its console")
        console = start_console(launch_console, passphrase, clock)
        import_env(shell, console, passphrase, clock)
        agent_session(shell, console, passphrase, clock)
        auto_approved_run(shell, console, clock)
        denied_request(shell, console, clock)
    finally:
        clock.log("Stopping the console and the shell")
        if "console" in panes:
            panes["console"].send(CTRL_C)
            time.sleep(0.3)
            panes["console"].send(CTRL_C)
            time.sleep(1.5)
        for pane in panes.values():
            pane.stop()
        shutil.rmtree(work)
    output.parent.mkdir(parents=True, exist_ok=True)
    events = {name: {"columns": pane.size[0], "rows": pane.size[1], "events": pane.events} for name, pane in panes.items()}
    output.write_text(json.dumps({"marks": clock.marks, "panes": events}))
    clock.log(f"Wrote {output}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the envh demo's two terminals")
    parser.add_argument("output", type=Path, nargs="?", default=Path("build/demo/recording.json"))
    record(parser.parse_args().output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
