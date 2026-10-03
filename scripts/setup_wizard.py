#!/usr/bin/env python3
"""envh setup: install or upgrade, console, .env import, leftover scan and Claude Code, one step at a time.

Every step says what it will change and waits for a yes. Runs as you, with the standard library only, because envh
is not installed yet when it starts. Rerunning it is safe: finished steps are detected.

The install step reruns this file as root (`--install-as-root`, under the system /usr/bin/python3) for the part that
needs root: copying the source to a root-only folder, building /opt/envh from that copy, then `envh install`. envh
never runs from this folder, because agents can edit it and the console runs the code that holds the keys.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BUILD_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")
CLAUDE_SOURCE = REPO / "claude"
CLAUDE_HOME = Path.home() / ".claude"
HOOK_FILE_NAME = "envh_ask.py"
INSTALL_PREFIX = Path("/opt/envh")
INSTALL_RECORD = INSTALL_PREFIX / "installed-from"
ENVH_BIN = Path("/usr/local/bin/envh")
ROOT_FLAG = "--install-as-root"
SUDO = "/usr/bin/sudo"
SYSTEM_PYTHON = "/usr/bin/python3"
SERVICE_USER = "envh"
SUDO_SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
UV_ROOT_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | /usr/bin/sudo env UV_UNMANAGED_INSTALL=/usr/local/bin sh"
PREFLIGHT_FAILURE = "fix the problems above"
CLAUDE_MD_MARKER = "API keys are managed by `envh`"


class Quit(Exception):
    pass


def say(text: str = "") -> None:
    print(text, flush=True)


def ask_yes(question: str) -> bool:
    while True:
        answer = input(f"{question} [y/N/q] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        if answer in ("q", "quit"):
            raise Quit
        say("answer y, n, or q to stop the wizard")


def ask_text(question: str) -> str:
    answer = input(f"{question} ").strip()
    if answer.lower() in ("q", "quit"):
        raise Quit
    return answer


def run_shown(command: list[str]) -> int:
    say("  $ " + shlex.join(command))
    return subprocess.run(command).returncode


def run_shown_capturing(command: list[str]) -> tuple[int, str]:
    say("  $ " + shlex.join(command))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    for line in process.stdout:
        print(line, end="", flush=True)
        lines.append(line)
    return process.wait(), "".join(lines)


def show_diff(old_text: str, new_text: str, path: Path) -> None:
    for line in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), f"{path} (now)", f"{path} (after)", lineterm="", n=2):
        say("    " + line)


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


def console_status() -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(ENVH_BIN), "status"], capture_output=True, text=True)


def root_owned_uv() -> Path | None:
    found = shutil.which("uv", path=SUDO_SECURE_PATH)
    if found and Path(found).stat().st_uid == 0:
        return Path(found)
    return None


def folder_source() -> tuple[str, list[str]]:
    commit = git_output("log", "-1", "--format=%h %s (%cs)")
    if commit is None:
        return f"{REPO} (not a git checkout)", []
    changes = (git_output("status", "--porcelain") or "").splitlines()
    return f"{commit} from {REPO}" + (" with uncommitted changes" if changes else ""), changes


def installed_source() -> str | None:
    try:
        return INSTALL_RECORD.read_text().strip()
    except OSError:
        return None


def root_install_command(label: str, flags: list[str]) -> list[str]:
    return [SUDO, SYSTEM_PYTHON, str(Path(__file__).resolve()), ROOT_FLAG, "--source", label, *flags]


def ensure_uv() -> bool:
    found = root_owned_uv()
    if found:
        say(f"uv: {found} (root-owned, good)")
        return True
    say("The install runs uv as root to build /opt/envh. A uv in your home folder can be replaced by anything")
    say("running as you, agents included, before root runs it. This puts a root-owned uv in /usr/local/bin")
    say("(it downloads astral.sh's installer and runs it as root; it does not touch shell profiles):")
    say(f"  $ {UV_ROOT_INSTALL}")
    if ask_yes("Install a root-owned uv now?"):
        if subprocess.run(UV_ROOT_INSTALL, shell=True).returncode != 0 or root_owned_uv() is None:
            raise SystemExit("installing uv failed; see the output above")
        return True
    user_uv = Path.home() / ".local" / "bin" / "uv"
    if user_uv.exists() and ask_yes(f"Use {user_uv} instead?"):
        return True
    say("No uv to install with; skipping the install.")
    return False


def step_install() -> str:
    say("Installs envh root-owned under /opt/envh, creates the 'envh' service user that owns the vault, and sets your")
    say("vault passphrase and approval password. envh never runs from this folder: root copies it to /opt/envh,")
    say("because agents can edit this folder and the console runs the code that holds your keys.")
    fresh_install = not envh_installed()
    label, changes = folder_source()
    installed = installed_source()
    say(f"  this folder: {label}")
    for line in changes:
        say(f"    {line}")
    if changes:
        say("  root runs these uncommitted changes too; check them before you go on")
    if fresh_install:
        question = "Install envh from this folder?"
    else:
        say(f"  installed:   {installed or 'unknown'}")
        if installed == label and not changes:
            question = "envh is already installed from this commit. Reinstall it anyway?"
        else:
            question = "Upgrade envh to this folder? The vault, the passwords and the policy are kept."
    if not ask_yes(question):
        return "skipped" if fresh_install else "kept the installed version"
    try:
        if not ensure_uv():
            return "skipped (no uv)"
        flags: list[str] = []
        say()
        say("Dry run: runs the preflight checks and prints every change, building in a scratch folder that is")
        say("deleted afterwards. sudo asks for your password.")
        returncode, output = run_shown_capturing(root_install_command(label, ["--dry-run", *flags]))
        if returncode != 0 and PREFLIGHT_FAILURE in output:
            say()
            say("The preflight found the 'problem:' lines above; each says what is wrong and how to fix it.")
            say("Fix them and rerun the wizard, or accept them only if you have checked each one yourself.")
            if not ask_yes("Accept them and continue with --ignore-preflight?"):
                return "stopped at preflight"
            flags.append("--ignore-preflight")
            returncode, output = run_shown_capturing(root_install_command(label, ["--dry-run", *flags]))
        if returncode != 0:
            raise SystemExit("the dry run failed; see the output above")
        say()
        if fresh_install:
            say("The install asks for three things:")
            say("  - your sudo password;")
            say("  - a vault passphrase: encrypts the vault on disk, typed every time you start the console. It is")
            say("    stored nowhere: forget it and the stored keys are gone (you would import them again);")
            say("  - an approval password: typed on the console to approve each request and to change secrets or")
            say("    policy. Make it different from the passphrase and from your sudo password. Only a hash is kept.")
            say("Then it prints a console phrase (three words). The real console always shows it; write it down.")
        if not ask_yes("Make the changes listed in the dry run?"):
            return "skipped after the dry run"
        if run_shown(root_install_command(label, flags)) != 0:
            raise SystemExit("the install failed; see the output above")
        if fresh_install:
            ask_text("Write down the console phrase printed above, then press Enter.")
            return "installed"
        say("Restart the console to run the new code: the running broker keeps the old code until then.")
        return "upgraded"
    finally:
        say("Forgetting the sudo password typed in this terminal:")
        run_shown([SUDO, "-k"])


def stage_build_files(destination: Path) -> None:
    shutil.copytree(REPO / "src", destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in BUILD_FILES:
        shutil.copy2(REPO / name, destination / name)


def build_environment(uv: str, source: Path, prefix: Path, python_dir: Path | None) -> None:
    """Build prefix/env from source with uv. A Python that uv downloads goes to python_dir; for an install that every user runs it
    must not be uv's default under root's home, which only root can open."""
    environment = {**os.environ, "HOME": pwd.getpwuid(os.geteuid()).pw_dir, "UV_PROJECT_ENVIRONMENT": str(prefix / "env")}
    if python_dir is not None:
        environment["UV_PYTHON_INSTALL_DIR"] = str(python_dir)
    command = [uv, "sync", "--project", str(source), "--frozen", "--no-dev", "--no-editable", "--python", "3.12", "--quiet"]
    subprocess.run(command, env=environment, check=True)


def uv_for_root(invoking_user: str) -> str:
    found = shutil.which("uv")
    if found:
        return found
    user_uv = Path(pwd.getpwnam(invoking_user).pw_dir) / ".local" / "bin" / "uv"
    if user_uv.exists():
        return str(user_uv)
    raise SystemExit(f"uv not found. A root-owned copy is safest:  {UV_ROOT_INSTALL}")


def link_envh_bin(target: Path) -> None:
    temp_link = ENVH_BIN.with_name(".envh-link-tmp")
    temp_link.unlink(missing_ok=True)
    temp_link.symlink_to(target)
    os.replace(temp_link, ENVH_BIN)


def install_as_root(argv: list[str]) -> int:
    """The root half of the install step. The dry run builds into a scratch folder so `envh install --dry-run` can print its plan."""
    parser = argparse.ArgumentParser(prog=f"setup_wizard.py {ROOT_FLAG}", description="the wizard's root half; run the wizard instead")
    parser.add_argument("--source", required=True, help="where the code comes from, recorded in /opt/envh/installed-from")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ignore-preflight", action="store_true")
    args = parser.parse_args(argv)
    invoking_user = os.environ.get("SUDO_USER")
    if os.geteuid() != 0 or not invoking_user:
        raise SystemExit(f"{ROOT_FLAG} is the wizard's root half; run python3 scripts/setup_wizard.py instead")
    uv = uv_for_root(invoking_user)
    install_flags = [flag for flag, wanted in (("--dry-run", args.dry_run), ("--ignore-preflight", args.ignore_preflight)) if wanted]
    with tempfile.TemporaryDirectory(prefix="envh-install-") as scratch:
        staging = Path(scratch) / "source"
        say(f"copying the build files of {REPO} to a root-only staging folder")
        stage_build_files(staging)
        if args.dry_run:
            prefix = Path(scratch) / "envh"
            say(f"dry run: building in {prefix} instead of {INSTALL_PREFIX}, so that envh install can print its plan")
            build_environment(uv, staging, prefix, None)
        else:
            prefix = INSTALL_PREFIX
            say(f"building {prefix}: Python 3.12 and the dependency versions pinned in uv.lock")
            build_environment(uv, staging, prefix, prefix / "python")
            subprocess.run(["chown", "-R", "root:root", str(prefix)], check=True)
            subprocess.run(["chmod", "-R", "go-w", str(prefix)], check=True)
            INSTALL_RECORD.write_text(args.source + "\n")
            os.chmod(INSTALL_RECORD, 0o644)
            link_envh_bin(prefix / "env" / "bin" / "envh")
            say(f"linked {ENVH_BIN} -> {prefix / 'env' / 'bin' / 'envh'}")
        say("running envh install")
        return subprocess.run([str(prefix / "env" / "bin" / "envh"), "install", *install_flags]).returncode


def step_console() -> str:
    if not ENVH_BIN.exists():
        say("envh is not installed; skipping.")
        return "skipped (envh not installed)"
    if console_status().returncode == 0:
        say("The console is already running.")
        return "already running"
    say("The console is the only place where requests get approved. It runs as the envh user, in its own window.")
    say("Open a NEW terminal window (not a pane inside Claude Code or another AI tool), or click the")
    say("'envh console' launcher, and run:")
    say()
    say("    sudo envh-console")
    say()
    say("It asks for your sudo password, prints the console phrase (check it matches what you wrote down), then")
    say("asks for the vault passphrase. Leave that window open: closing it ends every session.")
    if os.environ.get("XDG_SESSION_TYPE") == "x11":
        say("This is an X11 session: programs running as you can read and type into that window. A text console")
        say("(Ctrl-Alt-F3, log in, `exec sudo envh-console`) avoids that; see the README section")
        say("'Who else can see and type into the console'.")
    while True:
        if ask_text("Press Enter once it says 'console ready' (s to skip):").lower() == "s":
            return "skipped"
        status = console_status()
        if status.returncode == 0:
            say("Connected to the console.")
            return "running"
        say(f"  not reachable yet: {status.stderr.strip() or f'exit code {status.returncode}'}")


def step_import() -> str:
    if not ENVH_BIN.exists() or console_status().returncode != 0:
        say("The console is not running, and the import needs it to store keys; skipping.")
        return "skipped (console not running)"
    say("Moves API keys out of .env files into the vault; start with one project and add others any time later")
    say("with `envh import <folder>` or by rerunning this wizard. The import finds .env files (not .env.example),")
    say("shows a plan with names and fingerprints (never values) and a diff for each file, and asks before it")
    say("writes. Then the console asks you to approve, each original file is backed up, and the files are rewritten.")
    answer = ask_text("Folders or .env files to import from, separated by spaces (Enter to skip):")
    if not answer:
        return "skipped"
    returncode = run_shown([str(ENVH_BIN), "import", *(os.path.expanduser(path) for path in shlex.split(answer))])
    if returncode != 0:
        return f"ended with exit code {returncode}"
    say("Once the rewritten files work, delete the backup folder printed above: it holds the old values in plaintext.")
    return "imported"


def step_scan() -> str:
    if not ENVH_BIN.exists():
        say("envh is not installed; skipping.")
        return "skipped (envh not installed)"
    say("Read-only: looks for key-shaped strings outside .env files (shell history, Claude Code transcripts,")
    say("notebooks, other repos). Prints file, line, key type and a short fingerprint, never a value.")
    answer = ask_text("Folders to scan, separated by spaces (Enter for your home folder, s to skip):")
    if answer.lower() == "s":
        return "skipped"
    returncode = run_shown([str(ENVH_BIN), "scan", *(os.path.expanduser(path) for path in shlex.split(answer))])
    if returncode == 0:
        return "nothing found"
    if returncode == 1:
        say("The copies above are readable by anything running as you. Delete them, or rotate those keys.")
        return "found copies"
    return f"failed with exit code {returncode}"


def offer_copy(label: str, source: Path, destination: Path) -> str:
    new_text = source.read_text()
    if destination.exists():
        if destination.read_text() == new_text:
            say(f"  {destination} is already up to date")
            return f"{label} already in place"
        say(f"  {destination} exists and differs from this version:")
        show_diff(destination.read_text(), new_text, destination)
        question = f"Replace {destination}?"
    else:
        say(f"  copy {source.relative_to(REPO)} to {destination} (new file, {len(new_text.splitlines())} lines)")
        question = f"Create {destination}?"
    if not ask_yes(question):
        return f"{label} skipped"
    write_atomically(destination, new_text)
    say(f"  wrote {destination}")
    return f"{label} installed"


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


def render_settings(settings: dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def offer_settings_hook(settings_path: Path, hook_command: str) -> str:
    original_text = settings_path.read_text() if settings_path.exists() else ""
    try:
        settings = json.loads(original_text) if original_text.strip() else {}
        merged = with_envh_hook(settings, hook_command)
        existing = envh_hook_commands(settings)
    except (json.JSONDecodeError, ValueError, AttributeError) as error:
        raise SystemExit(f"cannot add the hook to {settings_path}: {error}. Fix the file and rerun the wizard.") from error
    if existing:
        say(f"  {settings_path} already runs the envh hook: {existing[0]}")
        return "settings hook already in place"
    new_text = render_settings(merged)
    say(f"  add a PreToolUse hook for Bash to {settings_path}. Every line that changes:")
    show_diff(original_text, new_text, settings_path)
    if original_text and render_settings(settings) != original_text:
        say("  (the file is rewritten with 2-space indentation, so lines whose formatting differs show up too)")
    if not ask_yes(f"Update {settings_path}?"):
        return "settings skipped"
    if settings_path.exists():
        backup = settings_path.with_name(f"{settings_path.name}.before-envh-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(settings_path, backup)
        say(f"  backup of the old file: {backup}")
    write_atomically(settings_path, new_text)
    say(f"  wrote {settings_path}; new Claude Code sessions pick it up")
    return "settings hook added"


def claude_md_with_snippet(current: str, snippet: str) -> str:
    if not current:
        return snippet
    return current + ("\n" if current.endswith("\n") else "\n\n") + snippet


def offer_claude_md(path: Path, snippet: str) -> str:
    current = path.read_text() if path.exists() else ""
    if CLAUDE_MD_MARKER in current:
        say(f"  {path} already has the envh lines")
        return "CLAUDE.md already in place"
    say(f"  append these lines to {path}, which Claude Code reads in every project:")
    for line in snippet.splitlines():
        say(f"    + {line}")
    if not ask_yes(f"Append to {path}?"):
        return "CLAUDE.md skipped"
    write_atomically(path, claude_md_with_snippet(current, snippet))
    say(f"  wrote {path}")
    return "CLAUDE.md added"


def step_claude_code() -> str:
    say(f"Four changes in {CLAUDE_HOME}, each asked separately. Skip this step if you do not use Claude Code.")
    hook_destination = CLAUDE_HOME / "hooks" / HOOK_FILE_NAME
    results: list[str] = []
    say()
    say("a) Skill: teaches the agent to request a session with a reason, run commands inside it, and stop when denied.")
    results.append(offer_copy("skill", CLAUDE_SOURCE / "skills" / "envh" / "SKILL.md", CLAUDE_HOME / "skills" / "envh" / "SKILL.md"))
    say()
    say("b) Hook script: asks you in the app before envh session starts, imports, preset proposals and session-less")
    say("   runs, so you know when to look at the console; refuses sudo, su, doas and pkexec from the agent.")
    results.append(offer_copy("hook script", CLAUDE_SOURCE / "hooks" / HOOK_FILE_NAME, hook_destination))
    say()
    say("c) Settings: tells Claude Code to run the hook script before every Bash command.")
    if hook_destination.exists():
        results.append(offer_settings_hook(CLAUDE_HOME / "settings.json", f"python3 {hook_destination}"))
    else:
        say(f"  skipped: {hook_destination} is not in place")
        results.append("settings skipped")
    say()
    say("d) CLAUDE.md: three lines telling the agent never to read .env files and to use envh for keys.")
    results.append(offer_claude_md(CLAUDE_HOME / "CLAUDE.md", (CLAUDE_SOURCE / "CLAUDE.snippet.md").read_text()))
    return "; ".join(results)


STEPS: list[tuple[str, str, Callable[[], str]]] = [
    ("Install or upgrade envh", "/opt/envh, the envh service user, console helper, passwords (sudo)", step_install),
    ("Start the console", "you open it in its own window; the wizard waits until it answers", step_console),
    ("Import .env files", "moves keys into the vault, with a diff and a backup of each file", step_import),
    ("Scan for leftover copies", "read-only search of history, transcripts, notebooks and other repos", step_scan),
    ("Connect Claude Code", "skill, hook, settings.json entry and CLAUDE.md lines in ~/.claude", step_claude_code),
]


def refuse_unsafe_start() -> None:
    if not sys.platform.startswith("linux"):
        raise SystemExit("envh runs on Linux only")
    if not sys.stdin.isatty():
        raise SystemExit("run the wizard in a terminal: it asks questions")
    if os.geteuid() == 0:
        raise SystemExit("run the wizard as yourself, not as root: python3 scripts/setup_wizard.py (it asks for sudo when needed)")
    if os.environ.get("CLAUDECODE"):
        raise SystemExit("this terminal belongs to Claude Code. Run the wizard in a separate terminal window: a sudo password typed here would be usable by the agent.")


def print_summary(results: list[tuple[str, str]]) -> None:
    say()
    say("=== Summary ===")
    for title, result in results:
        say(f"  {title}: {result}")
    for title, _, _ in STEPS[len(results):]:
        say(f"  {title}: not reached")
    say("Rerun the wizard any time: to finish skipped steps, to upgrade after `git pull`, or to import more projects.")


def main() -> int:
    if sys.argv[1:2] == [ROOT_FLAG]:
        return install_as_root(sys.argv[2:])
    refuse_unsafe_start()
    say("envh setup. The steps:")
    for number, (title, summary, _) in enumerate(STEPS, 1):
        say(f"  {number}. {title}: {summary}")
    say()
    say("Each step says what it will change and asks first. Enter means no; q stops the wizard.")
    say("sudo remembers your password for the terminal it was typed in, so run this in a terminal window of its")
    say("own, never in a terminal pane of Claude Code or another AI tool.")
    if not ask_yes("Is this a separate terminal window that no AI agent can type into?"):
        say("Open a new terminal window and run the wizard there.")
        return 1
    results: list[tuple[str, str]] = []
    try:
        for number, (title, _, step) in enumerate(STEPS, 1):
            say()
            say(f"=== Step {number}/{len(STEPS)}: {title} ===")
            results.append((title, step()))
    except (Quit, KeyboardInterrupt):
        say()
        say("stopped")
    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
