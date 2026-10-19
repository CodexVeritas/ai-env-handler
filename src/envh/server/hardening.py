"""Process hardening and startup assertions for the broker."""

from __future__ import annotations

import fcntl
import os
import resource
import sys
from pathlib import Path

from envh.platform import PlatformError, ancestor_terminals, ancestors, legacy_tiocsti_enabled, make_non_dumpable, process_owner

LOCK_FILE = "lock"
TERMINAL_SHARERS = ("tmux", "screen", "zellij", "dtach", "abduco")


class HardeningError(RuntimeError):
    pass


def harden_process() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Raise the open-file soft limit to the hard limit so a burst of client connections cannot
    # exhaust the broker's descriptors at the low default (the control server also caps concurrent
    # connections; this widens the headroom below that cap).
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    os.umask(0o077)
    make_non_dumpable()


def assert_terminal_is_ours() -> None:
    """sudo can run the console on a pseudo-terminal of its own and relay what is typed into the terminal it was
    started from, so the terminals of the console's ancestors must be locked too."""
    if not sys.stdin.isatty():
        raise HardeningError("the console must run on a terminal (stdin is not a tty)")
    try:
        ancestors = ancestor_terminals(os.getpid())
    except (OSError, PlatformError) as error:
        raise HardeningError(f"can't check the terminals the console was started from: {error}") from error
    for terminal in dict.fromkeys([Path(os.ttyname(sys.stdin.fileno())), *ancestors]):
        owner = terminal.stat().st_uid
        if owner != os.geteuid():
            raise HardeningError(
                f"the terminal {terminal} is owned by uid {owner}, not by this process (uid {os.geteuid()}); start the console with `sudo envh-console`, and if you did, rerun the setup wizard to update it"
            )


def shared_terminal_warning() -> str | None:
    """tmux, screen and the like keep the terminal the console reads from, and let anything running as the user who
    started them see and type into it. A warning, not a refusal, like X11, which does the same for a whole desktop."""
    try:
        found = [(pid, name) for pid, name in ancestors(os.getpid()) if name.split(":")[0].strip().lower() in TERMINAL_SHARERS]
        foreign = [(pid, name) for pid, name in found if process_owner(pid) != 0]
    except (OSError, PlatformError) as error:
        return f"Can't check whether the console runs inside tmux or screen: {error}"
    if not foreign:
        return None
    pid, name = foreign[0]
    return f"The console runs inside {name} (pid {pid}): programs you run can see and type into it. Start it in a window of its own."


def assert_no_tiocsti() -> None:
    state = legacy_tiocsti_enabled()
    if state is None:
        raise HardeningError("this kernel has no dev.tty.legacy_tiocsti control; Linux 6.2 or newer is required")
    if state:
        raise HardeningError("dev.tty.legacy_tiocsti is enabled; set it to 0 (sysctl) before running the console")


def acquire_instance_lock(data_dir: Path) -> int:
    descriptor = os.open(data_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise HardeningError("another envh broker is already running for this data directory") from error
    return descriptor
