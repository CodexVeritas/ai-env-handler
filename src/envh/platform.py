"""OS-level details in one place: service user, paths, peer credentials, hardening primitives, preflight checks."""

from __future__ import annotations

import ctypes
import grp
import os
import pwd
import socket
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

SERVICE_USER = "envh"
RUNTIME_DIR = Path("/run/envh")
SOCKET_NAME = "ctl.sock"
SOCKET_ENV_VAR = "ENVH_SOCKET"
ROOT_GRANTING_GROUPS = ("docker", "lxd", "disk")
PR_SET_DUMPABLE = 4


class PlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int

    def cmdline(self) -> str:
        try:
            raw = Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            return "(command line unavailable)"
        parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return " ".join(parts) if parts else "(empty command line)"


def socket_path() -> Path:
    override = os.environ.get(SOCKET_ENV_VAR)
    if override:
        return Path(override)
    return RUNTIME_DIR / SOCKET_NAME


def service_user_home() -> Path:
    try:
        return Path(pwd.getpwnam(SERVICE_USER).pw_dir)
    except KeyError as error:
        raise PlatformError(f"service user {SERVICE_USER!r} does not exist; run `sudo envh install`") from error


def peer_credentials(sock: socket.socket) -> PeerCredentials:
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def make_non_dumpable() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise PlatformError(f"prctl(PR_SET_DUMPABLE, 0) failed with errno {errno}")


def legacy_tiocsti_enabled() -> bool | None:
    path = Path("/proc/sys/dev/tty/legacy_tiocsti")
    if not path.exists():
        return None
    return path.read_text().strip() != "0"


def yama_ptrace_scope() -> int | None:
    path = Path("/proc/sys/kernel/yama/ptrace_scope")
    if not path.exists():
        return None
    return int(path.read_text().strip())


def root_granting_groups_of(username: str) -> list[str]:
    memberships = [group.gr_name for group in grp.getgrall() if username in group.gr_mem]
    return [name for name in memberships if name in ROOT_GRANTING_GROUPS]


def sudo_listing(username: str) -> str:
    result = subprocess.run(["sudo", "-n", "-l", "-U", username], capture_output=True, text=True)
    if result.returncode != 0:
        raise PlatformError(f"`sudo -n -l -U {username}` failed: {result.stderr.strip() or f'exit code {result.returncode}'}")
    return result.stdout


def passwordless_sudo_entries(listing: str) -> list[str]:
    return [line.strip() for line in listing.splitlines() if "NOPASSWD" in line or "!authenticate" in line]


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def preflight_problems(invoking_user: str | None) -> list[str]:
    problems: list[str] = []
    tiocsti = legacy_tiocsti_enabled()
    if tiocsti is None:
        problems.append("kernel has no dev.tty.legacy_tiocsti control (needs Linux 6.2 or newer): terminal input injection cannot be ruled out")
    elif tiocsti:
        problems.append("dev.tty.legacy_tiocsti is enabled: run `sudo sysctl -w dev.tty.legacy_tiocsti=0` and persist it in /etc/sysctl.d")
    scope = yama_ptrace_scope()
    if scope is None:
        problems.append("Yama LSM is not available: same-user ptrace is not restricted")
    elif scope < 1:
        problems.append("kernel.yama.ptrace_scope is 0: set it to 1 in /etc/sysctl.d")
    if invoking_user:
        groups = root_granting_groups_of(invoking_user)
        if groups:
            problems.append(f"user {invoking_user} is in root-granting group(s) {', '.join(groups)}: remove with `sudo gpasswd -d {invoking_user} <group>` and log in again")
        try:
            entries = passwordless_sudo_entries(sudo_listing(invoking_user))
        except PlatformError as error:
            problems.append(f"could not check the sudo rules of {invoking_user}: {error}")
        else:
            if entries:
                listed = "".join(f"\n  {entry}" for entry in entries)
                problems.append(
                    f"{invoking_user} can run these with sudo without a password, so agents can too:{listed}\n"
                    "Fine only if each is a fixed root-owned command that reads no file you can write."
                )
    return problems


def preflight_warnings() -> list[str]:
    warnings: list[str] = []
    if session_type() == "x11":
        warnings.append("X11 session: programs you run can read and type into the console window (README: 'Who else can see and type into the console')")
    return warnings
