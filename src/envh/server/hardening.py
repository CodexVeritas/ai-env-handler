"""Process hardening and startup assertions for the broker."""

from __future__ import annotations

import fcntl
import os
import resource
import sys
from pathlib import Path

from envh.platform import PlatformError, ancestor_terminals, legacy_tiocsti_enabled, make_non_dumpable

LOCK_FILE = "lock"


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
