"""Process hardening and startup assertions for the broker."""

from __future__ import annotations

import fcntl
import os
import resource
import sys
from pathlib import Path

from envh.platform import legacy_tiocsti_enabled, make_non_dumpable

LOCK_FILE = "lock"


class HardeningError(RuntimeError):
    pass


def harden_process() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    make_non_dumpable()


def assert_terminal_is_ours() -> None:
    if not sys.stdin.isatty():
        raise HardeningError("the console must run on a terminal (stdin is not a tty)")
    owner = os.stat(os.ttyname(sys.stdin.fileno())).st_uid
    if owner != os.geteuid():
        raise HardeningError(
            f"the terminal is owned by uid {owner}, not by this process (uid {os.geteuid()}); start the console with `sudo envh-console`, which fixes ownership"
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
