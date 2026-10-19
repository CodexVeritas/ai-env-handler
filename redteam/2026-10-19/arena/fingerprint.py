"""Emit a reproducibility fingerprint for the arena run.

Prints a JSON object capturing the exact configuration a finding was reproduced against, so a
result is pinned to {target commit, kernel, hardening-gate sysctls, display server, python,
pyrage}. The hash of this object is the arena's environment id, recorded beside every finding.

Stdlib only; safe to run as any user. Facts it cannot read (e.g. a missing Yama node) are
reported as null, which is itself meaningful for the hardening-gate tiers.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

# This file is copied out of the repo into the arena scripts dir, so the repo path comes from the
# environment (set by bootstrap), falling back to the cwd.
REPO = Path(os.environ.get("ENVH_ARENA_REPO") or os.getcwd())


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=str(REPO))
        return out.stdout.strip() or None
    except Exception:
        return None


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def facts() -> dict[str, object]:
    pyrage_version = None
    try:
        from importlib.metadata import version

        pyrage_version = version("pyrage")
    except Exception:
        pyrage_version = None
    return {
        "git_commit": _run(["git", "rev-parse", "HEAD"]),
        "git_dirty": bool(_run(["git", "status", "--porcelain"])),
        "kernel": platform.release(),
        "uname": platform.version(),
        "legacy_tiocsti": _read("/proc/sys/dev/tty/legacy_tiocsti"),
        "yama_ptrace_scope": _read("/proc/sys/kernel/yama/ptrace_scope"),  # null => Yama absent
        "dev_uinput_present": Path("/dev/uinput").exists(),
        "display_server": os.environ.get("XDG_SESSION_TYPE") or ("x11" if os.environ.get("DISPLAY") else ("wayland" if os.environ.get("WAYLAND_DISPLAY") else None)),
        "python": sys.version.split()[0],
        "pyrage": pyrage_version,
    }


def main() -> int:
    data = facts()
    env_id = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
    print(json.dumps({"environment_id": env_id, **data}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
