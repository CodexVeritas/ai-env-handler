"""The envh client: standard library only, so it can be copied anywhere. Talks to the broker over its Unix socket."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from envh.tools.importer import run_wizard
from envh.tools.scanner import main as scan_main, skipped_folders_help
from envh.platform import socket_path as default_socket_path
from envh.client.transport import EXIT_USAGE, ClientError, Connection, waiting_notice

SESSION_ENV_VAR = "ENVH_SESSION"
RUN_MARKER_VAR = "ENVH_RUN_MARKER"
LINGER_GRACE_SECONDS = 2.0
CONSOLE_COMMANDS = (
    ("add SECRET", "add a secret, or replace its value"),
    ("rm SECRET", "remove a secret"),
    ("edit presets", "add, change or remove presets"),
    ("preset rm NAME", "remove a preset"),
    ("edit config", "change approval rules and session limits"),
)


def parse_list(values: list[str] | None) -> list[str]:
    items: list[str] = []
    for value in values or []:
        items.extend(part.strip() for part in value.split(",") if part.strip())
    return items


def cmd_run(args: argparse.Namespace, sock: Path) -> int:
    if not args.command:
        raise ClientError("nothing to run: put the command after `--`", EXIT_USAGE)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    session_id = args.session or os.environ.get(SESSION_ENV_VAR)
    with Connection(sock) as conn:
        conn.send(op="run", session=session_id, presets=parse_list(args.presets), reason=args.reason, command=command, **{"with": parse_list(args.with_)})
        reply = conn.recv_ok()
        if reply.get("pending"):
            with waiting_notice(reply, None):
                reply = conn.recv_ok()
        env = reply["env"]
        print(f"envh: run #{reply['run_id']} with {', '.join(sorted(env))}", file=sys.stderr, flush=True)
        marker = secrets.token_hex(16)
        exit_code = spawn(command, {**env, RUN_MARKER_VAR: marker})
        lingering = [] if args.keep_background else terminate_lingering(marker)
        if lingering:
            print(f"envh: terminated {len(lingering)} process(es) the run left behind that still held the values: pids {', '.join(map(str, lingering))}", file=sys.stderr)
        try:
            conn.send(op="run_done", exit_code=exit_code, lingering_terminated=len(lingering))
        except OSError:
            print("envh: the broker went away during the run; the run is recorded as ended by disconnect", file=sys.stderr)
    return exit_code


def spawn(command: list[str], extra_env: dict[str, str]) -> int:
    try:
        child = subprocess.Popen(command, env={**os.environ, **extra_env})
    except FileNotFoundError as error:
        raise ClientError(f"command not found: {command[0]}", EXIT_USAGE) from error

    def forward(signum: int, _frame: object) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, forward)
    returncode = child.wait()
    return 128 - returncode if returncode < 0 else returncode


def processes_with_marker(marker: str) -> list[int]:
    needle = f"{RUN_MARKER_VAR}={marker}".encode()
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            environment = Path(f"/proc/{entry}/environ").read_bytes()
        except OSError:
            continue
        if needle in environment.split(b"\0"):
            found.append(int(entry))
    return found


def terminate_lingering(marker: str) -> list[int]:
    """Stop every process from this run that is still alive after the command exited, so nothing keeps the values."""
    lingering = processes_with_marker(marker)
    if not lingering:
        return []
    for pid in lingering:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + LINGER_GRACE_SECONDS
    while time.monotonic() < deadline and processes_with_marker(marker):
        time.sleep(0.05)
    for pid in processes_with_marker(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return lingering


def cmd_session_start(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(
            op="session_start",
            presets=parse_list(args.presets),
            minutes=args.minutes,
            reason=args.reason,
            command=sys.argv,
            **{"with": parse_list(args.with_)},
        )
        reply = conn.recv_ok()
        if reply.get("excluded_per_run"):
            print(f"envh: per-run secrets are not covered by sessions: {', '.join(reply['excluded_per_run'])}", file=sys.stderr, flush=True)
        with waiting_notice(reply, f"envh session wait {reply['request_id']}"):
            final = conn.recv_ok()
    return print_session(final, args.quiet)


def cmd_session_wait(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="session_wait", request_id=args.request_id)
        reply = conn.recv_ok()
        with waiting_notice(reply, f"envh session wait {args.request_id}") if reply.get("pending") else nullcontext():
            final = conn.recv_ok()
    return print_session(final, args.quiet)


def print_session(reply: dict[str, Any], quiet: bool) -> int:
    session_id = reply["session_id"]
    if quiet:
        print(session_id)
        return 0
    print(f"session {session_id} approved, expires {reply['expires_at']}")
    print(f"use it with:  envh run --session {session_id} --reason \"...\" -- <command>")
    print(f"or export it: export {SESSION_ENV_VAR}={session_id}")
    return 0


def cmd_session_end(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="session_end", session_id=args.session_id or os.environ.get(SESSION_ENV_VAR))
        conn.recv_ok()
    print("session ended")
    return 0


def cmd_list(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="list")
        payload = conn.recv_ok()
    print("SECRETS")
    for entry in payload["secrets"]:
        print(f"  {entry['name']:<34} {entry['approval']:<8} max session {entry['max_session']}")
    print("PRESETS")
    for preset in payload["presets"] or []:
        print(f"  {preset['name']}   (max session {preset['max_session']})")
        for entry in preset["env"]:
            flags = []
            if entry["approval"] == "per-run":
                flags.append("per-run")
            if not entry["in_vault"]:
                flags.append("MISSING FROM VAULT")
            suffix = f"   [{', '.join(flags)}]" if flags else ""
            print(f"      {entry['var']:<30} <- {entry['secret']}{suffix}")
    if not payload["presets"]:
        print("  (none)")
    print("LIVE SESSIONS")
    for session in payload["sessions"]:
        print(f"  {session['id']}  {', '.join(session['presets']) or '(ad hoc)':<24} expires {session['expires_at']}  vars {', '.join(session['vars'])}  reason: {session['reason'] or '-'}")
    if not payload["sessions"]:
        print("  (none)")
    return 0


def cmd_status(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="status")
        payload = conn.recv_ok()
    print(json.dumps(payload, indent=2))
    return 0


def cmd_manage(args: argparse.Namespace, sock: Path) -> int:
    print("Secrets and presets change only on the envh console. Type one of these there:")
    print()
    for command, purpose in CONSOLE_COMMANDS:
        print(f"  {command:<18}{purpose}")
    print()
    print("Each asks for your vault passphrase.")
    print("Or write presets in a file here and send them to the console: envh preset propose FILE")
    print()
    if console_running(sock):
        print("✓ The console is running.")
    else:
        print("! The console isn't running. Start it in its own terminal window, not an AI tool's terminal:")
        print("    sudo envh-console")
    return 0


def console_running(sock: Path) -> bool:
    try:
        Connection(sock).close()
    except ClientError:
        return False
    return True


def cmd_preset_validate(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="preset_validate", yaml=Path(args.file).read_text())
        reply = conn.recv_ok()
    print(f"valid: {', '.join(reply['presets'])}")
    return 0


def cmd_preset_propose(args: argparse.Namespace, sock: Path) -> int:
    with Connection(sock) as conn:
        conn.send(op="preset_propose", yaml=Path(args.file).read_text(), reason=args.reason)
        reply = conn.recv_ok()
        with waiting_notice(reply, None):
            conn.recv_ok()
    print(f"presets applied: {', '.join(reply['presets'])}")
    return 0


def cmd_import(args: argparse.Namespace, sock: Path) -> int:
    return run_wizard(args, sock)


def cmd_scan(args: argparse.Namespace, sock: Path) -> int:
    return scan_main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="envh", description="Human-approved secrets for scripts and agents")
    parser.add_argument("--socket", type=Path, default=None, help=f"broker socket (default {default_socket_path()})")
    commands = parser.add_subparsers(dest="command_name", required=True)

    run = commands.add_parser("run", help="run a command with secrets in its environment")
    run.add_argument("--session", help=f"session id (default: ${SESSION_ENV_VAR})")
    run.add_argument("--preset", dest="presets", action="append", metavar="PRESET[,PRESET...]", help="repeat or use commas to combine presets")
    run.add_argument("--with", dest="with_", action="append", metavar="VAR[=SECRET],...")
    run.add_argument("--reason", help="why you need these secrets; shown to the approver")
    run.add_argument("--keep-background", action="store_true", help="do not terminate processes the command leaves running (they keep the values)")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    session = commands.add_parser("session", help="start, wait for, or end a session")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    start = session_commands.add_parser("start")
    start.add_argument("presets", nargs="*", metavar="PRESET", help="one or more presets; their variables are combined")
    start.add_argument("--with", dest="with_", action="append", metavar="VAR[=SECRET],...")
    start.add_argument("--minutes", type=int, required=True)
    start.add_argument("--reason")
    start.add_argument("--quiet", action="store_true", help="print only the session id")
    start.set_defaults(func=cmd_session_start)
    wait = session_commands.add_parser("wait")
    wait.add_argument("request_id", type=int)
    wait.add_argument("--quiet", action="store_true")
    wait.set_defaults(func=cmd_session_wait)
    end = session_commands.add_parser("end")
    end.add_argument("session_id", nargs="?")
    end.set_defaults(func=cmd_session_end)

    commands.add_parser("list", help="secrets, presets and live sessions").set_defaults(func=cmd_list)
    commands.add_parser("status", help="pending requests, runs, sessions").set_defaults(func=cmd_status)
    commands.add_parser("manage", help="how to add, change or remove secrets and presets").set_defaults(func=cmd_manage)

    preset = commands.add_parser("preset", help="validate or propose presets")
    preset_commands = preset.add_subparsers(dest="preset_command", required=True)
    validate = preset_commands.add_parser("validate")
    validate.add_argument("file")
    validate.set_defaults(func=cmd_preset_validate)
    propose = preset_commands.add_parser("propose")
    propose.add_argument("file")
    propose.add_argument("--reason")
    propose.set_defaults(func=cmd_preset_propose)

    importer = commands.add_parser("import", help="move secrets out of .env files, step by step")
    importer.add_argument("paths", nargs="+", help="a directory to scan or .env files")
    importer.add_argument("--dry-run", action="store_true")
    importer.add_argument("--depth", type=int, default=3)
    importer.set_defaults(func=cmd_import)

    scan = commands.add_parser("scan", help="find where key-shaped strings live (paths and fingerprints, never values)", epilog=skipped_folders_help())
    scan.add_argument("paths", nargs="*", help="directories or files to scan (default: your home directory)")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--max-size", type=int, default=25, help="skip files larger than this many MB (default 25)")
    scan.set_defaults(func=cmd_scan)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sock = args.socket or default_socket_path()
    try:
        return args.func(args, sock)
    except ClientError as error:
        print(f"envh: {error}", file=sys.stderr)
        return error.exit_code
    except KeyboardInterrupt:
        print("envh: interrupted", file=sys.stderr)
        return 130
