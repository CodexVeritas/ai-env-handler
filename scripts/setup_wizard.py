#!/usr/bin/env python3
"""envh setup: install or update, console, .env import, stray-key scan and Claude Code, one step at a time.

Runs as you with the standard library only, because envh is not installed yet when it starts. Rerunning is safe:
finished steps are detected. Each step prints one short sentence; `?` at a question shows a few more lines, and the
README holds the full details, so keep it that way when adding copy.

The install step reruns this file as root (`--install-as-root`, under /usr/bin/python3) to check the system, build
/opt/envh from a root-only copy of the source and run `envh install`. envh never runs from this folder, because agents
can edit it and the console runs the code that holds the keys.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import time
import tty
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parents[1]
BUILD_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")
CLAUDE_SOURCE = REPO / "claude"
CLAUDE_HOME = Path.home() / ".claude"
HOOK_FILE_NAME = "envh_ask.py"
INSTALL_PREFIX = Path("/opt/envh")
INSTALL_RECORD = INSTALL_PREFIX / "installed-from"
ENVH_BIN = Path("/usr/local/bin/envh")
ROOT_FLAG = "--install-as-root"
PREFLIGHT_EXIT = 3
SUDO = "/usr/bin/sudo"
SYSTEM_PYTHON = "/usr/bin/python3"
SERVICE_USER = "envh"
SUDO_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
UV_ROOT_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | /usr/bin/sudo env UV_UNMANAGED_INSTALL=/usr/local/bin sh"
CLAUDE_MD_MARKER = "API keys are managed by `envh`"
INTERACTIVE = sys.stdout.isatty()
COLOR = INTERACTIVE and "NO_COLOR" not in os.environ
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DETAIL_LEVELS = ["Essentials only", "Every detail: what each step changes, and why"]
verbose = False

INSTALL_MORE = """Copies envh to /opt/envh, where only root can change it.
Adds a system user, envh, that owns your encrypted keys.
Adds two commands: envh, and sudo envh-console.
Changes none of your existing settings.
Details: README, "What the install changes\""""
UNCOMMITTED_MORE = "Root installs exactly what is in this folder, including these files:"
UV_MORE = """envh is built with uv, run as root. A uv in your home folder could be swapped by anything running
as you, agents included, before root runs it. This runs astral.sh's installer as root, into /usr/local/bin.
It changes no shell settings."""
USER_UV_MORE = "Less safe: anything running as you, agents included, could replace it before root runs it."
PREFLIGHT_MORE = """Each line above says what to fix. These gaps let a program running as you become root,
and then read your keys. Continue only if you have checked each one yourself.
Details: README, "What the install changes\""""
CONSOLE_MORE = """It asks for your sudo password, shows your console phrase, then asks for your vault passphrase.
Keep that window open: closing it ends every session.
You can also open it from the "envh console" app launcher."""
X11_MORE = 'On X11, other programs you run can read and type into that window. See README, "Who else can see".'
IMPORT_MORE = """Shows what will change and asks before writing anything.
Backs up each .env file first.
Names keys after the project, like MYAPP_OPENAI_API_KEY.
Import more projects later with: envh import <folder>"""
SCAN_MORE = """Checks shell history, Claude Code transcripts, notebooks and other repos.
Skips installed packages, caches and browser profiles; the report lists what it skipped.
Shows where each copy is, never the key itself.
Scan somewhere else with: envh scan <folder>"""


class Quit(Exception):
    pass


class Outcome(NamedTuple):
    done: bool
    text: str


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(text: str) -> str:
    return paint("1", text)


def dim(text: str) -> str:
    return paint("2", text)


def say(text: str = "") -> None:
    print(text, flush=True)


def line(text: str) -> None:
    say("  " + text)


def ok(text: str) -> None:
    line(paint("32", "✓") + " " + text)


def warn(text: str) -> None:
    line(paint("33", "!") + " " + text.replace("\n", "\n    "))


def note(text: str) -> None:
    line(dim("· " + text))


def detail(text: str) -> None:
    if verbose:
        note(text)


def show_more(more: str) -> None:
    for text in more.splitlines():
        line(dim("  " + text))


def ask(question: str, more: str, default: bool = True) -> bool:
    choices = "[Y/n/?]" if default else "[y/N/?]"
    say()
    if verbose:
        show_more(more)
        say()
    while True:
        answer = input(f"  {bold(question)} {dim(choices)} ").strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("q", "quit"):
            raise Quit
        if answer == "?":
            show_more(more)
            say()
            continue
        line("Answer y or n, ? for more, or q to quit.")


def ask_line(question: str, more: str) -> str:
    say()
    if verbose:
        show_more(more)
        say()
    while True:
        answer = input(f"  {bold(question)} ").strip()
        if answer.lower() in ("q", "quit"):
            raise Quit
        if answer != "?":
            return answer
        show_more(more)
        say()


def choose(question: str, options: list[str]) -> int:
    """Pick an option with the arrow keys (or j/k, or its number) and Enter."""
    say()
    line(bold(question))
    selected = 0
    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor)

    def draw() -> None:
        for index, option in enumerate(options):
            pointer, text = (paint("36", "›"), option) if index == selected else (" ", dim(option))
            sys.stdout.write(f"\r\033[K    {pointer} {text}\n")
        sys.stdout.flush()

    try:
        tty.setcbreak(descriptor)
        draw()
        while True:
            key = os.read(descriptor, 3)
            if key in (b"\r", b"\n"):
                return selected
            if key in (b"q", b"Q"):
                raise Quit
            if key in (b"\x1b[A", b"k"):
                selected = (selected - 1) % len(options)
            elif key in (b"\x1b[B", b"j"):
                selected = (selected + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                selected = int(key) - 1
            sys.stdout.write(f"\033[{len(options)}A")
            draw()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def write_atomically(path: Path, text: str) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(target.name + ".envh-tmp")
    temp_path.write_text(text)
    if target.exists():
        os.chmod(temp_path, stat.S_IMODE(target.stat().st_mode))
    os.replace(temp_path, target)


def git_output(*arguments: str) -> str | None:
    if shutil.which("git") is None:
        return None
    result = subprocess.run(["git", "-C", str(REPO), *arguments], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def envh_installed() -> bool:
    try:
        pwd.getpwnam(SERVICE_USER)
        return True
    except KeyError:
        return ENVH_BIN.exists()


def console_running() -> bool:
    return ENVH_BIN.exists() and subprocess.run([str(ENVH_BIN), "status"], capture_output=True).returncode == 0


def root_owned_uv() -> Path | None:
    found = shutil.which("uv", path=SUDO_SECURE_PATH)
    if found and Path(found).stat().st_uid == 0:
        return Path(found)
    return None


def folder_source() -> tuple[str, list[str]]:
    commit = git_output("log", "-1", "--format=%h (%cs)")
    if commit is None:
        return f"{REPO} (not a git checkout)", []
    changes = (git_output("status", "--porcelain") or "").splitlines()
    return commit + (" + uncommitted changes" if changes else ""), changes


def installed_source() -> str | None:
    try:
        return INSTALL_RECORD.read_text().strip()
    except OSError:
        return None


def root_install_command(label: str, flags: list[str]) -> list[str]:
    return [SUDO, SYSTEM_PYTHON, str(Path(__file__).resolve()), ROOT_FLAG, "--source", label, *flags, *(["--verbose"] if verbose else [])]


def ensure_uv() -> bool:
    if root_owned_uv():
        return True
    say()
    line("envh is built with uv, and root needs its own copy.")
    if ask("Install uv for root?", UV_MORE):
        detail(f"Running: {UV_ROOT_INSTALL}")
        result = subprocess.run(UV_ROOT_INSTALL, shell=True, capture_output=True, text=True)
        if result.returncode != 0 or root_owned_uv() is None:
            say(result.stdout + result.stderr)
            raise SystemExit("✗ Installing uv failed; see the output above.")
        ok("uv installed for root")
        return True
    user_uv = Path.home() / ".local" / "bin" / "uv"
    if user_uv.exists() and ask(f"Use your own uv ({tilde(user_uv)}) instead?", USER_UV_MORE, default=False):
        return True
    return False


def step_install() -> Outcome:
    label, changes = folder_source()
    installed = installed_source()
    fresh_install = not envh_installed()
    if not fresh_install and installed == label and not changes:
        ok("envh is up to date")
        return Outcome(True, "up to date")
    if fresh_install:
        line("Installs envh where agents can't change it. Needs your sudo password.")
        question, more = "Install envh?", INSTALL_MORE
    else:
        line("Updates envh to the version in this folder. Your keys and settings stay.")
        question, more = "Update envh?", f"Installed:   {installed or 'unknown'}\nThis folder: {label}"
    if not ask(question, more):
        return Outcome(False, "skipped")
    if changes:
        say()
        warn("This folder has uncommitted changes. They will be installed too.")
        if not ask("Install them?", "\n".join([UNCOMMITTED_MORE, *changes]), default=False):
            return Outcome(False, "skipped (uncommitted changes)")
    try:
        if not ensure_uv():
            return Outcome(False, "skipped (no uv)")
        say()
        detail(f"Running as root: {SYSTEM_PYTHON} {tilde(Path(__file__).resolve())} {ROOT_FLAG}")
        returncode = subprocess.run(root_install_command(label, [])).returncode
        if returncode == PREFLIGHT_EXIT:
            if not ask("Continue anyway?", PREFLIGHT_MORE, default=False):
                return Outcome(False, "stopped at the system checks")
            say()
            returncode = subprocess.run(root_install_command(label, ["--ignore-preflight"])).returncode
        if returncode != 0:
            raise SystemExit("✗ The install failed; see the error above.")
    finally:
        subprocess.run([SUDO, "-k"])
        detail("Ran sudo -k: this terminal no longer remembers your sudo password")
    if fresh_install:
        ask_line("Write down the console phrase, then press Enter.", "You'll check it each time you start the console.")
        return Outcome(True, "installed")
    if console_running():
        warn("Restart the console to use the new version.")
    return Outcome(True, "updated")


def stage_build_files(destination: Path) -> None:
    shutil.copytree(REPO / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in BUILD_FILES:
        shutil.copy2(REPO / name, destination / name)


def system_problems(source: Path, invoking_user: str) -> list[str]:
    """Run envh's preflight checks from the staged root-owned source, before anything is built or changed."""
    sys.path.insert(0, str(source))
    platform = importlib.import_module("envh.platform")
    return platform.preflight_problems(invoking_user)


def run_with_spinner(text: str, command: list[str], environment: dict[str, str]) -> None:
    if not INTERACTIVE:
        note(text)
    with tempfile.TemporaryFile("w+") as log:
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT, text=True)
        frame = 0
        while process.poll() is None:
            if INTERACTIVE:
                sys.stdout.write(f"\r  {SPINNER[frame % len(SPINNER)]} {text}")
                sys.stdout.flush()
            frame += 1
            time.sleep(0.1)
        if INTERACTIVE:
            sys.stdout.write("\r\033[K")
        if process.returncode != 0:
            log.seek(0)
            say(log.read())
            raise subprocess.CalledProcessError(process.returncode, command)


def build_environment(uv: str, source: Path, prefix: Path) -> None:
    """Build prefix/env from source with uv. A Python that uv downloads goes to prefix/python: uv's default under
    root's home only root can open, and every user runs this install."""
    environment = {
        **os.environ,
        "HOME": pwd.getpwuid(os.geteuid()).pw_dir,
        "UV_PROJECT_ENVIRONMENT": str(prefix / "env"),
        "UV_PYTHON_INSTALL_DIR": str(prefix / "python"),
    }
    command = [uv, "sync", "--project", str(source), "--frozen", "--no-dev", "--no-editable", "--python", "3.12", "--quiet"]
    run_with_spinner("Building envh (about a minute)", command, environment)


def uv_for_root(invoking_user: str) -> str:
    found = shutil.which("uv")
    if found:
        return found
    user_uv = Path(pwd.getpwnam(invoking_user).pw_dir) / ".local" / "bin" / "uv"
    if user_uv.exists():
        return str(user_uv)
    raise SystemExit("✗ uv not found. Rerun the wizard and let it install uv for root.")


def link_envh_bin(target: Path) -> None:
    temp_link = ENVH_BIN.with_name(".envh-link-tmp")
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(target)
    os.replace(temp_link, ENVH_BIN)


def install_as_root(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"setup_wizard.py {ROOT_FLAG}", description="the wizard's root half; run the wizard instead")
    parser.add_argument("--source", required=True, help="where the code comes from, recorded in /opt/envh/installed-from")
    parser.add_argument("--ignore-preflight", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    global verbose
    verbose = args.verbose
    invoking_user = os.environ.get("SUDO_USER")
    if os.geteuid() != 0 or not invoking_user:
        raise SystemExit(f"{ROOT_FLAG} is the wizard's root half; run python3 scripts/setup_wizard.py instead")
    uv = uv_for_root(invoking_user)
    with tempfile.TemporaryDirectory(prefix="envh-install-") as scratch:
        staging = Path(scratch) / "source"
        stage_build_files(staging)
        detail(f"Copied the source to a root-only folder: {staging}")
        problems = system_problems(staging / "src", invoking_user)
        if problems and not args.ignore_preflight:
            for problem in problems:
                warn(problem)
            return PREFLIGHT_EXIT
        if not problems:
            ok("System checks passed")
        build_environment(uv, staging, INSTALL_PREFIX)
    detail(f"Built {INSTALL_PREFIX / 'env'} with {uv}: Python 3.12 and the versions pinned in uv.lock")
    subprocess.run(["chown", "-R", "root:root", str(INSTALL_PREFIX)], check=True)
    subprocess.run(["chmod", "-R", "go-w", str(INSTALL_PREFIX)], check=True)
    detail(f"Made {INSTALL_PREFIX} owned and writable by root only")
    INSTALL_RECORD.write_text(args.source + "\n")
    os.chmod(INSTALL_RECORD, 0o644)
    detail(f"Recorded the version in {INSTALL_RECORD}")
    link_envh_bin(INSTALL_PREFIX / "env" / "bin" / "envh")
    detail(f"Linked {ENVH_BIN}")
    ok("envh built")
    install_flags = [flag for flag, wanted in (("--verbose", verbose), ("--ignore-preflight", args.ignore_preflight)) if wanted]
    return subprocess.run([str(ENVH_BIN), "install", *install_flags]).returncode


def step_console() -> Outcome:
    if not ENVH_BIN.exists():
        note("Skipped: envh isn't installed.")
        return Outcome(False, "skipped")
    if console_running():
        ok("The console is running")
        return Outcome(True, "running")
    line("The console is where you approve key requests. In a new terminal window, run:")
    say()
    say("      " + bold("sudo envh-console"))
    more = CONSOLE_MORE + ("\n" + X11_MORE if os.environ.get("XDG_SESSION_TYPE") == "x11" else "")
    while True:
        if ask_line('Press Enter once it says "console ready" (s to skip).', more).lower() == "s":
            return Outcome(False, "skipped")
        if console_running():
            ok("Connected to the console")
            return Outcome(True, "running")
        warn("Can't reach the console yet.")


def step_import() -> Outcome:
    if not console_running():
        note("Skipped: the console isn't running.")
        return Outcome(False, "skipped")
    line("Moves keys from a project's .env file into the vault. You can add more projects later.")
    answer = ask_line("Project folder to import (Enter to skip):", IMPORT_MORE)
    if not answer:
        return Outcome(False, "skipped")
    say()
    detail(f"Running: envh import {answer}")
    returncode = subprocess.run([str(ENVH_BIN), "import", *(os.path.expanduser(path) for path in shlex.split(answer))]).returncode
    if returncode != 0:
        return Outcome(False, f"stopped (exit code {returncode})")
    say()
    warn("Once the project works, delete the backup folder shown above. It holds the old keys.")
    return Outcome(True, "imported")


def step_scan() -> Outcome:
    if not ENVH_BIN.exists():
        note("Skipped: envh isn't installed.")
        return Outcome(False, "skipped")
    line("Looks for copies of keys left outside .env files. Read-only; can take a few minutes.")
    if not ask("Scan your home folder?", SCAN_MORE):
        return Outcome(False, "skipped")
    say()
    detail("Running: envh scan")
    returncode = subprocess.run([str(ENVH_BIN), "scan"]).returncode
    if returncode == 0:
        ok("No stray keys found")
        return Outcome(True, "nothing found")
    if returncode == 1:
        warn("Delete the copies above, or rotate those keys.")
        return Outcome(True, "found copies")
    return Outcome(False, f"failed (exit code {returncode})")


def envh_hook_commands(settings: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        for hook in entry.get("hooks", []):
            if HOOK_FILE_NAME in str(hook.get("command", "")):
                commands.append(hook["command"])
    return commands


def with_envh_hook(settings: Any, hook_command: str) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("settings.json does not hold a JSON object")
    if not isinstance(settings.get("hooks", {}), dict):
        raise ValueError("'hooks' in settings.json is not an object")
    if not isinstance(settings.get("hooks", {}).get("PreToolUse", []), list):
        raise ValueError("'hooks.PreToolUse' in settings.json is not a list")
    merged = copy.deepcopy(settings)
    entry = {"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command, "timeout": 10}]}
    merged.setdefault("hooks", {}).setdefault("PreToolUse", []).append(entry)
    return merged


def settings_with_hook(settings_path: Path, hook_command: str) -> str | None:
    """The new settings.json text, or None when an envh hook is already there."""
    original_text = settings_path.read_text() if settings_path.exists() else ""
    try:
        settings = json.loads(original_text) if original_text.strip() else {}
        merged = with_envh_hook(settings, hook_command)
        existing = envh_hook_commands(settings)
    except (json.JSONDecodeError, ValueError, AttributeError) as error:
        raise SystemExit(f"✗ Can't add the hook to {settings_path}: {error}. Fix the file and rerun the wizard.") from error
    return None if existing else json.dumps(merged, indent=2, ensure_ascii=False) + "\n"


def write_with_backup(path: Path, text: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.before-envh-{datetime.now():%Y%m%d-%H%M%S}"))
    write_atomically(path, text)


def claude_md_with_snippet(current: str, snippet: str) -> str:
    if not current:
        return snippet
    return current + ("\n" if current.endswith("\n") else "\n\n") + snippet


def claude_code_changes(claude_home: Path) -> list[tuple[str, Callable[[], None]]]:
    """Each change the Claude Code step would make, as (description, apply); empty when everything is in place."""
    changes: list[tuple[str, Callable[[], None]]] = []
    hook_destination = claude_home / "hooks" / HOOK_FILE_NAME
    copies = [
        (CLAUDE_SOURCE / "skills" / "envh" / "SKILL.md", claude_home / "skills" / "envh" / "SKILL.md", "the envh skill"),
        (CLAUDE_SOURCE / "hooks" / HOOK_FILE_NAME, hook_destination, "a hook that asks you before envh requests"),
    ]
    for source, destination, what in copies:
        text = source.read_text()
        if destination.exists() and destination.read_text() == text:
            continue
        verb = "Update" if destination.exists() else "Add"
        changes.append((f"{verb} {what}: {tilde(destination)}", lambda destination=destination, text=text: write_atomically(destination, text)))
    settings_path = claude_home / "settings.json"
    settings_text = settings_with_hook(settings_path, f"python3 {hook_destination}")
    if settings_text is not None:
        changes.append((f"Turn the hook on in {tilde(settings_path)} (backed up first)", lambda: write_with_backup(settings_path, settings_text)))
    claude_md = claude_home / "CLAUDE.md"
    current = claude_md.read_text() if claude_md.exists() else ""
    if CLAUDE_MD_MARKER not in current:
        snippet = (CLAUDE_SOURCE / "CLAUDE.snippet.md").read_text()
        changes.append((f"Add 3 lines to {tilde(claude_md)}", lambda: write_atomically(claude_md, claude_md_with_snippet(current, snippet))))
    return changes


def step_claude_code() -> Outcome:
    if not CLAUDE_HOME.exists():
        note("Skipped: Claude Code isn't set up for this user.")
        return Outcome(False, "skipped")
    changes = claude_code_changes(CLAUDE_HOME)
    if not changes:
        ok("Claude Code is already connected")
        return Outcome(True, "already connected")
    line("Teaches Claude Code to ask for keys through envh.")
    more = "\n".join(description for description, _ in changes) + '\nDetails: README, "Using it with Claude Code"'
    if not ask("Connect Claude Code?", more):
        return Outcome(False, "skipped")
    for description, apply in changes:
        apply()
        detail(description)
    ok("Connected. New Claude Code sessions use envh.")
    return Outcome(True, "connected")


STEPS: list[tuple[str, Callable[[], Outcome]]] = [
    ("Install envh", step_install),
    ("Start the console", step_console),
    ("Import keys", step_import),
    ("Find stray keys", step_scan),
    ("Connect Claude Code", step_claude_code),
]


def refuse_unsafe_start() -> None:
    if not sys.platform.startswith("linux"):
        raise SystemExit("envh runs on Linux only.")
    if not sys.stdin.isatty():
        raise SystemExit("Run the wizard in a terminal: it asks questions.")
    if os.geteuid() == 0:
        raise SystemExit("Run the wizard as yourself, without sudo. It asks for sudo when it needs it.")
    if os.environ.get("CLAUDECODE"):
        raise SystemExit("Run the wizard in a terminal window of its own, not inside Claude Code.")


def print_summary(results: list[tuple[str, Outcome]]) -> None:
    say()
    say()
    for title, outcome in results:
        mark = paint("32", "✓") if outcome.done else dim("·")
        line(f"{mark} {title:<20} {dim(outcome.text)}")
    for title, _ in STEPS[len(results):]:
        line(f"{dim('·')} {title:<20} {dim('not reached')}")
    say()
    line(dim("Rerun this any time to update envh or import more projects."))
    line(dim("To see or rename your keys, type keys in the console."))
    say()


def main() -> int:
    global verbose
    if sys.argv[1:2] == [ROOT_FLAG]:
        return install_as_root(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Install envh and connect it to your projects, one step at a time.")
    parser.add_argument("--verbose", action="store_true", help="show every detail without asking")
    args = parser.parse_args()
    refuse_unsafe_start()
    say()
    line(bold("envh setup"))
    line(dim("Keeps your API keys out of your AI agents' reach. Type ? at any question for more."))
    results: list[tuple[str, Outcome]] = []
    try:
        verbose = args.verbose or choose("How much do you want to see?", DETAIL_LEVELS) == 1
        for number, (title, step) in enumerate(STEPS, 1):
            say()
            say()
            say(paint("1;36", f"[{number}/{len(STEPS)}] {title}"))
            results.append((title, step()))
    except (Quit, KeyboardInterrupt):
        say()
        note("Stopped.")
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
