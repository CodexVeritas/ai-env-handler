#!/usr/bin/env python3
"""Runs the setup wizard end to end the way a person would, answering each prompt on a terminal of its own: installs
envh, starts the console with `sudo envh-console`, checks a client and the Claude Code hook reach it, closes the console's
terminal, checks one Ctrl-C stops the console at its passphrase prompt, then uninstalls.

Run by scripts/ci/install_matrix.sh as a user who can sudo with the password in CI_PASSWORD. Standard library only, so
it runs on each distribution's own python3. Any system check problem fails the run, after the remaining steps have
run, so one run shows everything that differs on that distribution."""

from __future__ import annotations

import json
import os
import pty
import re
import select
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

PASSWORD = os.environ["CI_PASSWORD"]
VAULT_PASSPHRASE = "ci vault passphrase"
SUDO_PROMPT = r"[Pp]assword[^:\n]*:"
TERMINAL_CODES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\r")
WIZARD_SECONDS = 900
CONSOLE_SECONDS = 120
CONSOLE_HELPER = "/usr/local/sbin/envh-console"
ENVH_BIN = "/usr/local/bin/envh"


class Failure(Exception):
    pass


class Terminal:
    """A program on a pseudo-terminal of its own, so sudo and getpass read from it as they would from a person's."""

    def __init__(self, name: str, command: list[str]) -> None:
        self.name = name
        self.text = ""
        self.consumed = 0
        self.echo = True
        self.closed = False
        print(f"\n--- {name}: {' '.join(command)}", flush=True)
        self.pid, self.descriptor = pty.fork()
        if self.pid == 0:
            os.execvp(command[0], command)

    def read(self, seconds: float) -> bool:
        """False once the program, or this driver, has closed the terminal."""
        if self.closed:
            time.sleep(seconds)
            return False
        ready, _, _ = select.select([self.descriptor], [], [], seconds)
        if not ready:
            return True
        try:
            chunk = os.read(self.descriptor, 65536).decode("utf-8", "replace")
        except OSError:
            return False
        if not chunk:
            return False
        if self.echo:
            sys.stdout.write(TERMINAL_CODES.sub("", chunk))
            sys.stdout.flush()
        self.text += TERMINAL_CODES.sub("", chunk)
        return True

    def type(self, text: str) -> None:
        os.write(self.descriptor, (text + "\r").encode())

    def answer_until(self, replies: list[tuple[str, str | Callable[[], str]]], until: str, seconds: float) -> None:
        """Answer each prompt the program waits at, until `until` shows. A prompt counts only at the end of the
        output, where the program stops to read."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pending = self.text[self.consumed :]
            finished = re.search(until, pending)
            if finished:
                self.consumed += finished.end()
                return
            for pattern, reply in replies:
                if re.search(f"(?:{pattern})[ \t]*$", pending):
                    self.consumed = len(self.text)
                    self.type(reply() if callable(reply) else reply)
                    break
            else:
                if not self.read(1.0):
                    raise Failure(f"{self.name} ended before showing {until!r}")
        raise Failure(f"{self.name} didn't show {until!r} within {seconds:.0f} seconds")

    def wait(self, seconds: float) -> int:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            finished, status = os.waitpid(self.pid, os.WNOHANG)
            if finished:
                self.read(0)
                return os.waitstatus_to_exitcode(status)
            self.read(0.5)
        raise Failure(f"{self.name} didn't exit within {seconds:.0f} seconds")

    def close(self) -> None:
        """Like closing the window: the programs on the terminal get a hangup."""
        os.close(self.descriptor)
        self.closed = True


def system_check_problems(wizard_output: str) -> list[str]:
    """The problems the install's system checks listed before asking whether to continue; warnings don't count."""
    if "Continue anyway?" not in wizard_output:
        return []
    checks = wizard_output.split("Continue anyway?")[0].split("Copied the source to a root-only folder")[-1]
    return [line.strip()[2:] for line in checks.splitlines() if line.strip().startswith("! ")]


def run(command: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    print(f"\n--- {' '.join(command)}", flush=True)
    result = subprocess.run(command, input=stdin, capture_output=True, text=True)
    print(result.stdout + result.stderr, flush=True)
    return result


def main() -> int:
    failures: list[str] = []
    consoles: list[Terminal] = []

    def full_path_if_told(command: str) -> list[str]:
        """The command by name, as the docs show it, unless the wizard said this sudo can't find it that way."""
        if f"sudo {CONSOLE_HELPER}" in wizard.text:
            return [CONSOLE_HELPER if command == "envh-console" else ENVH_BIN]
        return [command]

    def start_console() -> str:
        if consoles:
            raise Failure("the wizard can't reach the console it was told is ready")
        console = Terminal("console", ["sudo", *full_path_if_told("envh-console")])
        consoles.append(console)
        console.answer_until([(SUDO_PROMPT, PASSWORD), ("Vault passphrase:", VAULT_PASSPHRASE)], "Listening for requests", CONSOLE_SECONDS)
        console.echo = False
        return ""

    wizard = Terminal("wizard", ["python3", "scripts/setup_wizard.py", "--verbose"])
    replies: list[tuple[str, str | Callable[[], str]]] = [
        (SUDO_PROMPT, PASSWORD),
        (r"Install envh\? \[Y/n/\?\]", "y"),
        (r"Install them\? \[y/N/\?\]", "y"),
        (r"Install uv for root\? \[Y/n/\?\]", "y"),
        (r"Continue anyway\? \[y/N/\?\]", "y"),
        ("Passphrase:", VAULT_PASSPHRASE),
        ("Repeat it:", VAULT_PASSPHRASE),
        (r"Write down the console phrase, then press Enter\.", ""),
        (r'Press Enter once it says "console ready" \(s to skip\)\.', start_console),
        (r"Project folder to import \(Enter to skip\):", ""),
        (r"Connect Claude Code\? \[Y/n/\?\]", "y"),
        (r"Scan your home folder\? \[Y/n/\?\]", "n"),
    ]
    def install() -> None:
        wizard.answer_until(replies, "Rerun this any time", WIZARD_SECONDS)
        if wizard.wait(30) != 0:
            failures.append("the wizard exited with an error")
        failures.extend(f"system check: {problem}" for problem in system_check_problems(wizard.text))
        for expected in ("envh built", "envh installed", "Connected to the console", "Connected. New Claude Code sessions use envh."):
            if expected not in wizard.text:
                failures.append(f"the wizard never said {expected!r}")

    def use() -> None:
        if run(["envh", "status"]).returncode != 0:
            failures.append("`envh status` can't reach the console")
        if run(["envh", "list"]).returncode != 0:
            failures.append("`envh list` failed")
        run(["readlink", "-f", "/opt/envh/env/bin/python"])
        hook_input = json.dumps({"tool_name": "Bash", "tool_input": {"command": "envh run --preset demo --reason ci -- true"}})
        hook = run(["python3", str(Path.home() / ".claude" / "hooks" / "envh_ask.py")], hook_input)
        if hook.returncode != 0 or "Traceback" in hook.stderr:
            failures.append("the Claude Code hook failed on this python3")

    def close_console() -> None:
        for console in consoles:
            console.close()
            try:
                console.wait(30)
            finally:
                run(["ps", "-eo", "pid,ppid,user,tty,stat,args"])
        if run(["envh", "status"]).returncode == 0:
            failures.append("the console still answers after its terminal closed")

    def interrupt_console() -> None:
        console = Terminal("console, then Ctrl-C", ["sudo", *full_path_if_told("envh-console")])
        console.answer_until([(SUDO_PROMPT, PASSWORD)], "Vault passphrase:", CONSOLE_SECONDS)
        os.write(console.descriptor, b"\x03")
        try:
            status = console.wait(15)
        except Failure:
            failures.append("Ctrl-C at the vault passphrase prompt didn't stop the console")
            console.close()
            console.wait(30)
            return
        if status != 130:
            failures.append(f"Ctrl-C at the vault passphrase prompt exited with status {status}, not 130")
        if "Traceback" in console.text or not re.search(r"Stopped\.\s*$", console.text):
            failures.append('Ctrl-C at the vault passphrase prompt didn\'t end with just "Stopped."')

    def uninstall() -> None:
        terminal = Terminal("uninstall", ["sudo", *full_path_if_told("envh"), "uninstall", "--purge"])
        terminal.answer_until([(SUDO_PROMPT, PASSWORD)], "remove user envh", 60)
        if terminal.wait(60) != 0:
            failures.append("`sudo envh uninstall --purge` failed")

    for step in (install, use, close_console, interrupt_console, uninstall):
        try:
            step()
        except Failure as error:
            failures.append(str(error))
            if step is install:
                break

    print("\n=== Result", flush=True)
    for failure in failures:
        print(f"FAIL {failure}")
    if not failures:
        print("PASS: installed, console started, client and hook reached it, uninstalled")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
