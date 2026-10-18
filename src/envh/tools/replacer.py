"""Replace every copy of chosen keys in files with a placeholder, in place. Standard library only.

Each file is read now and scanned again, so only what is there at that moment is replaced, and it is written back byte
for byte apart from the replaced keys. There is no backup on purpose: a backup would be one more copy of the keys.
"""

from __future__ import annotations

import os
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from envh.tools.scanner import place_of, scan_text

IN_USE_SECONDS = 600
LEFT_FOR_HAND = {"private-key": "only its first line matches"}
LOG_FILE = "replaced.txt"


def placeholder(kind: str, fingerprint: str) -> str:
    """Safe in .env files, shells, JSON and YAML, and never reported by a later scan."""
    return f"envh-removed-{kind}-{fingerprint}"


@dataclass
class Outcome:
    replaced: dict[str, int] = field(default_factory=dict)
    copies: int = 0
    skipped: dict[str, str] = field(default_factory=dict)


def in_git_repo(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in path.parents)


def replace_in_file(path: Path, fingerprints: set[str], now: float) -> tuple[int, str]:
    """(copies replaced, why nothing was) for one file. A file changed in the last IN_USE_SECONDS is left alone, since a
    program, such as an agent writing its transcript, may still be writing it and would lose what it writes."""
    if place_of(str(path)) == "credential files":
        return 0, "an app signs in with it, so replacing the key would sign you out"
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            return 0, "a link"
        if now - before.st_mtime < IN_USE_SECONDS:
            return 0, "changed in the last 10 minutes; it may be in use"
        data = path.read_bytes()
    except OSError as error:
        return 0, f"can't read it: {error.strerror or error}"
    text = data.decode("utf-8", errors="surrogateescape")
    edits: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for hit in scan_text(path, text):
        if hit.fingerprint in fingerprints and hit.kind not in LEFT_FOR_HAND:
            edits[hit.line - 1].append((hit.column, hit.length, placeholder(hit.kind, hit.fingerprint)))
    if not edits:
        return 0, "the key isn't in it any more"
    lines = text.split("\n")
    for index, changes in edits.items():
        line = lines[index]
        for column, length, replacement in sorted(changes, reverse=True):
            line = line[:column] + replacement + line[column + length:]
        lines[index] = line
    temp_path = path.with_name(f".{path.name}.envh-{os.getpid()}")
    try:
        with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write("\n".join(lines).encode("utf-8", errors="surrogateescape"))
        os.chmod(temp_path, stat.S_IMODE(before.st_mode))
        after = path.lstat()
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            temp_path.unlink()
            return 0, "it changed while being replaced"
        os.replace(temp_path, path)
    except OSError as error:
        temp_path.unlink(missing_ok=True)
        return 0, f"can't write it: {error.strerror or error}"
    return sum(len(changes) for changes in edits.values()), ""


def replace_all(targets: dict[str, set[str]], log_folder: Path) -> Outcome:
    """Replace the chosen keys in each file, and log what was replaced (paths and ids, never values) beside the scan."""
    outcome = Outcome()
    now = time.time()
    log_lines: list[str] = []
    for path, fingerprints in sorted(targets.items()):
        count, problem = replace_in_file(Path(path), fingerprints, now)
        if problem:
            outcome.skipped[path] = problem
            continue
        outcome.replaced[path] = count
        outcome.copies += count
        log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {count:>3} replaced  ids {' '.join(sorted(fingerprints))}  {path}")
    if log_lines:
        with os.fdopen(os.open(log_folder / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as handle:
            handle.write("\n".join(log_lines) + "\n")
    return outcome
