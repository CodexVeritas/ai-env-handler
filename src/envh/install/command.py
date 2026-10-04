"""`envh install` / `envh uninstall`: system setup, run as root by scripts/setup_wizard.py after its system checks."""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

from envh.platform import SERVICE_USER, preflight_problems, preflight_warnings
from envh.server.init_cmd import run_init

HELPER_PATH = Path("/usr/local/sbin/envh-console")
DESKTOP_PATH = Path("/usr/share/applications/envh.desktop")
ENVH_BIN = Path("/usr/local/bin/envh")
DATA_DIR = Path("/var/lib/envh")

HELPER_SCRIPT = """#!/bin/sh
# envh-console: start the envh broker on this terminal as the service user.
# Run with: sudo envh-console
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo envh-console" >&2
    exit 1
fi
tty_path=$(tty) || { echo "envh-console needs a terminal" >&2; exit 1; }
mkdir -p /run/envh
chown SERVICE_USER:SERVICE_USER /run/envh
chmod 755 /run/envh
original_owner=$(stat -c %u "$tty_path")
original_mode=$(stat -c %a "$tty_path")
restore() { chown "$original_owner" "$tty_path" 2>/dev/null || true; chmod "$original_mode" "$tty_path" 2>/dev/null || true; }
trap restore EXIT INT TERM HUP
chown SERVICE_USER "$tty_path"
chmod 600 "$tty_path"
if command -v runuser >/dev/null 2>&1; then
    runuser -u SERVICE_USER -- ENVH_BIN serve "$@"
else
    sudo -u SERVICE_USER -H ENVH_BIN serve "$@"
fi
""".replace("SERVICE_USER", SERVICE_USER).replace("ENVH_BIN", str(ENVH_BIN))

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=envh console
Comment=Approve secret access for scripts and agents
Exec=TERMINAL_COMMAND
Icon=dialog-password
Terminal=false
Categories=Utility;Security;
"""

class InstallError(RuntimeError):
    pass


def say(text: str) -> None:
    print(text, flush=True)


def require_root(dry_run: bool) -> None:
    if os.geteuid() != 0 and not dry_run:
        raise InstallError("run as root: sudo envh install")


def user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def terminal_command() -> str | None:
    if shutil.which("gnome-terminal"):
        return f"gnome-terminal --title=envh -- sudo {HELPER_PATH}"
    if shutil.which("x-terminal-emulator"):
        return f"x-terminal-emulator -e sudo {HELPER_PATH}"
    return None


def run(command: list[str], dry_run: bool) -> None:
    if not dry_run:
        subprocess.run(command, check=True)


def write_root_file(path: Path, content: str, mode: int, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(content)
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)


def invoking_user_name() -> str:
    """The account the install is for: the one that ran sudo, or the caller itself for a dry run without root. Refused
    for a root shell, where the checks could not tell whose sudo rules and groups to examine."""
    name = os.environ.get("SUDO_USER")
    if name:
        return name
    if os.geteuid() != 0:
        return pwd.getpwuid(os.geteuid()).pw_name
    raise InstallError("run envh install with sudo from your own account, so the checks and the policy know whose account it is")


def install(args: argparse.Namespace) -> int:
    """Quiet by default; --verbose and --dry-run list each change.

    It runs the system checks itself, even when they ran before: this is the privileged entry point, so it refuses on a
    problem unless --ignore-preflight says each one was checked."""
    require_root(args.dry_run)
    invoking_user = invoking_user_name()
    if args.dry_run and os.geteuid() != 0 and DATA_DIR.exists() and not os.access(DATA_DIR, os.X_OK):
        raise InstallError(f"envh is already installed and only {SERVICE_USER} can open {DATA_DIR}; run the dry run with sudo")
    for warning in preflight_warnings():
        say("  ! " + warning)
    problems = preflight_problems(invoking_user)
    if problems and not args.ignore_preflight:
        for problem in problems:
            say("  ! " + problem.replace("\n", "\n    "))
        raise InstallError("fix the problems above, or pass --ignore-preflight once you have checked each one")
    if problems:
        say(f"  ! Going on despite {len(problems)} system check problem{'s' if len(problems) > 1 else ''}, as --ignore-preflight asks")
    if not ENVH_BIN.exists() and not args.dry_run:
        raise InstallError(f"{ENVH_BIN} is missing; run scripts/setup_wizard.py instead of `envh install` directly")

    def report(text: str) -> None:
        if args.verbose or args.dry_run:
            say(f"  · {text}")

    if args.dry_run:
        say("Dry run: nothing changes. The install would set up:")
    if user_exists(SERVICE_USER):
        report(f"System user {SERVICE_USER}: already there")
    else:
        report(f"System user {SERVICE_USER}, home {DATA_DIR} (only {SERVICE_USER} can open it)")
        run(["useradd", "--system", "--create-home", "--home-dir", str(DATA_DIR), "--shell", "/usr/sbin/nologin", SERVICE_USER], args.dry_run)
    run(["chmod", "700", str(DATA_DIR)], args.dry_run)
    report(f"{HELPER_PATH}: starts the console as {SERVICE_USER}")
    write_root_file(HELPER_PATH, HELPER_SCRIPT, 0o755, args.dry_run)
    command = terminal_command()
    if command and DESKTOP_PATH.parent.exists():
        report(f'{DESKTOP_PATH}: the "envh console" app launcher')
        write_root_file(DESKTOP_PATH, DESKTOP_ENTRY.replace("TERMINAL_COMMAND", command), 0o644, args.dry_run)
    else:
        report("No app launcher: no desktop terminal found")
    if args.dry_run:
        if not (DATA_DIR / "vault.age").exists():
            report(f"The vault and policy in {DATA_DIR} (users: {invoking_user}), after asking for a vault passphrase")
        return 0
    say("  ✓ envh installed")
    if (DATA_DIR / "vault.age").exists():
        report("Kept the vault, its passphrase and the policy")
        return 0
    if not sys.stdin.isatty():
        raise InstallError("creating the vault asks for a passphrase; run the setup wizard in a terminal")
    run_init(DATA_DIR, invoking_user)
    run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(DATA_DIR)], args.dry_run)
    report(f"Policy in {DATA_DIR}: config.yaml (users: {invoking_user}) and presets.yaml")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    require_root(args.dry_run)
    for path in (HELPER_PATH, DESKTOP_PATH, ENVH_BIN):
        say(f"  remove {path}")
        if not args.dry_run:
            path.unlink(missing_ok=True)
    say("  remove /opt/envh")
    if not args.dry_run:
        shutil.rmtree("/opt/envh", ignore_errors=True)
        shutil.rmtree("/run/envh", ignore_errors=True)
    if args.purge:
        say(f"  remove user {SERVICE_USER} and {DATA_DIR} (the vault!)")
        if not args.dry_run and user_exists(SERVICE_USER):
            subprocess.run(["userdel", "--remove", SERVICE_USER], check=True)
    else:
        say(f"  kept user {SERVICE_USER} and {DATA_DIR}; pass --purge to delete the vault too")
    return 0


def main(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"envh {command}")
    parser.add_argument("--dry-run", action="store_true", help="print every action without doing it")
    if command == "install":
        parser.add_argument("--verbose", action="store_true", help="list each change")
        parser.add_argument("--ignore-preflight", action="store_true", help="install even though the system checks found problems")
    else:
        parser.add_argument("--purge", action="store_true", help="also delete the service user and the vault")
    args = parser.parse_args(argv)
    try:
        return install(args) if command == "install" else uninstall(args)
    except (InstallError, subprocess.CalledProcessError) as error:
        print(f"envh {command}: {error}", file=sys.stderr)
        return 1
