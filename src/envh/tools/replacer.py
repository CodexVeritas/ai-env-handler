"""Replace every copy of chosen keys in files with a placeholder, in place. Standard library only.

Each file is read now and scanned again, so only what is there at that moment is replaced, and it is written back byte
for byte apart from the replaced keys. A copy the scan found only by searching for a stored key is replaced where the
scan saw it, if the same value is still there. There is no backup on purpose: a backup would be one more copy of the keys.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from envh.common import value_id
from envh.tools.scanner import place_of, scan_text

IN_USE_SECONDS = 600
LEFT_FOR_HAND = {"private-key": "only its first line matches"}
LOG_FILE = "replaced.txt"
LOG_LINE = re.compile(r"^\S+ \S+\s+\d+ replaced  ids ((?:[0-9a-f]{8} ?)+)  (.+)$")


def placeholder(kind: str, fingerprint: str) -> str:
    """Safe in .env files, shells, JSON and YAML, and never reported by a later scan."""
    return f"envh-removed-{kind}-{fingerprint}"


class Spot(NamedTuple):
    """Where the scan saw a copy, so it can be replaced there once the value is checked to be the same."""

    line: int
    column: int
    length: int
    fingerprint: str
    kind: str


class FileResult(NamedTuple):
    """What replacing in one file did: copies replaced, why nothing was, and the ids of the keys it replaced."""

    copies: int
    problem: str
    ids: frozenset[str] = frozenset()


@dataclass
class Outcome:
    replaced: dict[str, int] = field(default_factory=dict)
    ids: dict[str, frozenset[str]] = field(default_factory=dict)
    copies: int = 0
    skipped: dict[str, str] = field(default_factory=dict)


def in_git_repo(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in path.parents)


def still_there(lines: list[str], line: int, column: int, length: int, fingerprint: str) -> bool:
    """Whether the value the scan saw at this line and column is there now."""
    seen = lines[line - 1][column:column + length] if 0 < line <= len(lines) else ""
    return len(seen) == length and value_id(seen) == fingerprint


def without_overlaps(changes: list[tuple[int, int, str, str]]) -> list[tuple[int, int, str, str]]:
    """The changes to one line, as (column, length, kind, id), with duplicates dropped and, of two that overlap, only
    the first kept."""
    kept: list[tuple[int, int, str, str]] = []
    for change in sorted(set(changes)):
        if not kept or change[0] >= kept[-1][0] + kept[-1][1]:
            kept.append(change)
    return kept


def replace_in_file(path: Path, fingerprints: set[str], now: float, spots: list[Spot] | tuple[Spot, ...] = ()) -> FileResult:
    """What replacing the keys in one file did. A file changed in the last IN_USE_SECONDS is left alone, since a
    program, such as an agent writing its transcript, may still be writing it and would lose what it writes. A rewritten
    file keeps its timestamp, so envh's own rewrite never looks like that."""
    if place_of(str(path)) == "credential files":
        return FileResult(0, "an app signs in with it, so replacing the key would sign you out")
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            return FileResult(0, "a link")
        if now - before.st_mtime < IN_USE_SECONDS:
            return FileResult(0, "changed in the last 10 minutes; it may be in use")
        data = path.read_bytes()
    except OSError as error:
        return FileResult(0, f"can't read it: {error.strerror or error}")
    text = data.decode("utf-8", errors="surrogateescape")
    edits: dict[int, list[tuple[int, int, str, str]]] = defaultdict(list)
    for hit in scan_text(path, text):
        if hit.fingerprint in fingerprints and hit.kind not in LEFT_FOR_HAND:
            edits[hit.line - 1].append((hit.column, hit.length, hit.kind, hit.fingerprint))
    lines = text.split("\n")
    for spot in spots:
        if spot.fingerprint in fingerprints and spot.kind not in LEFT_FOR_HAND and still_there(lines, spot.line, spot.column, spot.length, spot.fingerprint):
            edits[spot.line - 1].append((spot.column, spot.length, spot.kind, spot.fingerprint))
    if not edits:
        return FileResult(0, "the key isn't in it any more")
    edits = {index: without_overlaps(changes) for index, changes in edits.items()}
    for index, changes in edits.items():
        line = lines[index]
        for column, length, kind, fingerprint in sorted(changes, reverse=True):
            line = line[:column] + placeholder(kind, fingerprint) + line[column + length:]
        lines[index] = line
    temp_path = path.with_name(f".{path.name}.envh-{os.getpid()}")
    try:
        with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write("\n".join(lines).encode("utf-8", errors="surrogateescape"))
        os.chmod(temp_path, stat.S_IMODE(before.st_mode))
        after = path.lstat()
        if (after.st_mtime_ns, after.st_size) != (before.st_mtime_ns, before.st_size):
            temp_path.unlink()
            return FileResult(0, "it changed while being replaced")
        os.replace(temp_path, path)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    except OSError as error:
        temp_path.unlink(missing_ok=True)
        return FileResult(0, f"can't write it: {error.strerror or error}")
    return FileResult(sum(len(changes) for changes in edits.values()), "", frozenset(change[3] for changes in edits.values() for change in changes))


def replaced_pairs(log_folder: Path) -> set[tuple[str, str]]:
    """Every (path, id) that replaced.txt says was replaced, so the explorer doesn't show those keys again."""
    try:
        lines = (log_folder / LOG_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    pairs: set[tuple[str, str]] = set()
    for line in lines:
        match = LOG_LINE.match(line)
        if match is None:
            raise ValueError(f"{log_folder / LOG_FILE} has a line envh didn't write: {line[:60]}")
        pairs.update((match.group(2), fingerprint) for fingerprint in match.group(1).split())
    return pairs


def replace_all(targets: dict[str, set[str]], log_folder: Path, spots: dict[str, list[Spot]] | None = None) -> Outcome:
    """Replace the chosen keys in each file, and log what was replaced (paths and ids, never values) beside the scan.
    Only the ids actually replaced in a file are logged, since the explorer stops showing what the log names."""
    outcome = Outcome()
    now = time.time()
    log_lines: list[str] = []
    for path, fingerprints in sorted(targets.items()):
        result = replace_in_file(Path(path), fingerprints, now, (spots or {}).get(path, ()))
        if result.problem:
            outcome.skipped[path] = result.problem
            continue
        outcome.replaced[path] = result.copies
        outcome.ids[path] = result.ids
        outcome.copies += result.copies
        log_lines.append(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {result.copies:>3} replaced  ids {' '.join(sorted(result.ids))}  {path}")
    if log_lines:
        with os.fdopen(os.open(log_folder / LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8") as handle:
            handle.write("\n".join(log_lines) + "\n")
    return outcome
