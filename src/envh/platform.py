"""OS-level details in one place: service user, paths, peer credentials, hardening primitives, preflight checks."""

from __future__ import annotations

import ctypes
import grp
import os
import pwd
import re
import shlex
import socket
import stat
import struct
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SERVICE_USER = "envh"
RUNTIME_DIR = Path("/run/envh")
SOCKET_NAME = "ctl.sock"
SOCKET_ENV_VAR = "ENVH_SOCKET"
ROOT_GRANTING_GROUPS = ("docker", "lxd", "incus-admin", "libvirt", "libvirtd", "microk8s", "disk")
KEY_READING_GROUPS = ("input",)
ROOT_DAEMON_SOCKETS = tuple(
    Path(path)
    for path in (
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/run/containerd/containerd.sock",
        "/run/podman/podman.sock",
        "/var/snap/lxd/common/lxd/unix.socket",
        "/var/lib/lxd/unix.socket",
        "/var/lib/incus/unix.socket",
    )
)
ROOT_RUN_FOLDERS = tuple(Path(path) for path in ("/usr/local", "/usr/local/bin", "/usr/local/sbin", "/opt", "/opt/envh"))
UINPUT = Path("/dev/uinput")
DOAS_CONFIG = Path("/etc/doas.conf")
DOAS_OPTIONS = ("nopass", "nolog", "persist", "keepenv")
SHARED_SUDO_TICKETS = ("!tty_tickets", "timestamp_type=global")
PR_SET_DUMPABLE = 4


class PlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int

    def cmdline(self) -> str:
        """The process's arguments, shell-quoted so the approver sees where each starts and ends; one argument holding
        spaces or `--` could otherwise pass for several. Empty arguments stay, as ''."""
        try:
            raw = Path(f"/proc/{self.pid}/cmdline").read_bytes()
        except OSError:
            return "(command line unavailable)"
        arguments = raw.rstrip(b"\0")
        if not arguments:
            return "(empty command line)"
        return shlex.join(part.decode("utf-8", "replace") for part in arguments.split(b"\0"))


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


def parent_and_terminal(pid: int) -> tuple[int, int]:
    """A process's parent pid and the device number of its controlling terminal (0 for none)."""
    after_name = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(after_name[1]), int(after_name[4])


def terminal_path(device: int) -> Path:
    for path in [*Path("/dev/pts").iterdir(), *Path("/dev").iterdir()]:
        try:
            status = path.lstat()
        except OSError:
            continue
        if stat.S_ISCHR(status.st_mode) and status.st_rdev == device:
            return path
    raise PlatformError(f"no node in /dev for terminal device {os.major(device)}:{os.minor(device)}")


def ancestor_terminals(pid: int) -> list[Path]:
    """The terminals the process's ancestors run on, nearest first."""
    devices: list[int] = []
    ancestor, _ = parent_and_terminal(pid)
    while ancestor > 1:
        ancestor, device = parent_and_terminal(ancestor)
        if device and device not in devices:
            devices.append(device)
    return [terminal_path(device) for device in devices]


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


def group_names_of(username: str) -> list[str]:
    """Every group of the account, its primary group included. Member lists from `grp.getgrall()` leave out the primary
    group and can leave out groups from LDAP or sssd, so the system is asked for the account's groups instead."""
    names: list[str] = []
    for gid in os.getgrouplist(username, pwd.getpwnam(username).pw_gid):
        try:
            names.append(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return names


def root_granting_groups_of(username: str) -> list[str]:
    return [name for name in group_names_of(username) if name in ROOT_GRANTING_GROUPS]


def writable_by(username: str, paths: Iterable[Path]) -> list[Path]:
    """The paths that exist and that the account can write. The kernel decides, as that account, so groups, ACLs and
    modes all count; root asks it through a child process that has switched to the account."""
    account = pwd.getpwnam(username)
    existing = [path for path in dict.fromkeys(path.resolve() for path in paths) if os.path.exists(path)]
    if os.geteuid() == account.pw_uid:
        return [path for path in existing if os.access(path, os.W_OK)]
    if os.geteuid() != 0:
        raise PlatformError(f"only root or {username} can check what {username} can write")
    groups = os.getgrouplist(username, account.pw_gid)
    return [
        path
        for path in existing
        if subprocess.run(["test", "-w", str(path)], user=account.pw_uid, group=account.pw_gid, extra_groups=groups, capture_output=True).returncode == 0
    ]


def sudo_listing(username: str) -> str:
    result = subprocess.run(["sudo", "-n", "-l", "-U", username], capture_output=True, text=True)
    if result.returncode != 0:
        raise PlatformError(f"`sudo -n -l -U {username}` failed: {result.stderr.strip() or f'exit code {result.returncode}'}")
    return result.stdout


def passwordless_sudo_entries(listing: str) -> list[str]:
    return [line.strip() for line in listing.splitlines() if "NOPASSWD" in line or "!authenticate" in line]


def sudo_defaults(listing: str) -> list[str]:
    """The Defaults entries a `sudo -l` listing applies to the user, without spaces; sudo may wrap them over indented lines."""
    entries: list[str] = []
    in_defaults = False
    for line in listing.splitlines():
        if line.startswith("Matching Defaults entries"):
            in_defaults = True
        elif not line.strip() or not line[0].isspace():
            in_defaults = False
        elif in_defaults:
            entries.extend(entry.replace(" ", "") for entry in line.split(",") if entry.strip())
    return entries


def sudo_default_problems(username: str, defaults: list[str]) -> list[str]:
    problems: list[str] = []
    shared = [entry for entry in defaults if entry in SHARED_SUDO_TICKETS]
    if shared:
        problems.append(
            f"sudo's {', '.join(shared)} lets every terminal of {username} reuse a sudo password typed in one, so agents can run sudo after you start the console: remove it with visudo"
        )
    if not any(entry.startswith("secure_path=") for entry in defaults):
        problems.append(
            "sudo has no secure_path, so `sudo envh-console` runs the first envh-console in your PATH, which programs you run can change: "
            "add a secure_path with visudo, or start the console with `sudo /usr/local/sbin/envh-console`"
        )
    return problems


def doas_nopass_rules(config_text: str, username: str, groups: list[str]) -> list[str]:
    """The `permit nopass` rules of a doas.conf that match the account by name or by one of its groups."""
    identities = {username, *(f":{group}" for group in groups)}
    rules: list[str] = []
    for line in config_text.splitlines():
        rule = line.split("#", 1)[0].strip()
        words = re.sub(r"setenv\s*\{[^}]*\}", " ", rule).split()
        if words[:1] != ["permit"]:
            continue
        options = []
        for word in words[1:]:
            if word not in DOAS_OPTIONS:
                break
            options.append(word)
        identity = words[1 + len(options)] if len(words) > 1 + len(options) else ""
        if "nopass" in options and identity in identities:
            rules.append(rule)
    return rules


def session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def sudo_problems(username: str) -> list[str]:
    try:
        listing = sudo_listing(username)
    except PlatformError as error:
        return [f"could not check the sudo rules of {username}: {error}"]
    problems: list[str] = []
    entries = passwordless_sudo_entries(listing)
    if entries:
        listed = "".join(f"\n  {entry}" for entry in entries)
        problems.append(
            f"{username} can run these with sudo without a password, so agents can too:{listed}\n"
            "Fine only if each is a fixed root-owned command that reads no file you can write."
        )
    return problems + sudo_default_problems(username, sudo_defaults(listing))


def doas_problems(username: str) -> list[str]:
    try:
        config_text = DOAS_CONFIG.read_text()
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f"could not check {DOAS_CONFIG} for rules that skip the password: {error}"]
    rules = doas_nopass_rules(config_text, username, group_names_of(username))
    if not rules:
        return []
    listed = "".join(f"\n  {rule}" for rule in rules)
    return [f"{username} can run doas without a password, so agents can too; remove nopass from these lines in {DOAS_CONFIG}:{listed}"]


def group_name(gid: int) -> str | None:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def write_access_problems(username: str, reported_groups: list[str]) -> list[str]:
    """Sockets reached through a group already reported as root-granting are left out, so one cause gets one fix."""
    try:
        sockets = writable_by(username, ROOT_DAEMON_SOCKETS)
        folders = writable_by(username, ROOT_RUN_FOLDERS)
    except PlatformError as error:
        return [f"could not check which root-owned sockets and folders {username} can write: {error}"]
    problems = [
        f"{username} can use {path}, the socket of a daemon that runs containers as root, so agents can become root: remove the group or ACL that grants it"
        for path in sockets
        if group_name(path.stat().st_gid) not in reported_groups
    ]
    problems.extend(
        f"{username} can write {path}, where root runs envh from, so agents could replace that code: `sudo chown root:root {path} && sudo chmod go-w {path}`"
        for path in folders
    )
    return problems


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
        problems.extend(sudo_problems(invoking_user))
        problems.extend(doas_problems(invoking_user))
        problems.extend(write_access_problems(invoking_user, groups))
    return problems


def preflight_warnings(invoking_user: str | None) -> list[str]:
    warnings: list[str] = []
    if session_type() == "x11":
        warnings.append("X11 session: programs you run can read and type into the console window (README: 'Who else can see and type into the console')")
    if not invoking_user:
        return warnings
    key_reading = [name for name in group_names_of(invoking_user) if name in KEY_READING_GROUPS]
    if key_reading:
        warnings.append(
            f"{invoking_user} is in group(s) {', '.join(key_reading)}: programs you run can read every key you type, the vault passphrase included (README: 'Who else can see and type into the console')"
        )
    try:
        uinput_writable = writable_by(invoking_user, [UINPUT])
    except PlatformError as error:
        warnings.append(f"could not check whether {invoking_user} can write {UINPUT}: {error}")
    else:
        if uinput_writable:
            warnings.append(f"{invoking_user} can write {UINPUT}: programs you run can type into the console (README: 'Who else can see and type into the console')")
    return warnings
