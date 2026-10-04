"""`envh scan`: find where key-shaped strings live on this machine, without ever printing a value. Standard library only."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TextIO

# Provider formats that are distinctive enough to recognize anywhere, including transcripts and shell history, each with
# the literal markers one of which every match contains. A file holding none of a pattern's markers skips that pattern;
# most files hold none, which is what keeps the scan fast.
VALUE_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("openrouter", r"sk-or-v1-[a-f0-9]{64}", ("sk-or-v1-",)),
    ("anthropic", r"sk-ant-[A-Za-z0-9_-]{32,}", ("sk-ant-",)),
    ("openai", r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{32,}", ("sk-proj-", "sk-svcacct-", "sk-admin-")),
    ("openai-legacy", r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}", ("T3BlbkFJ",)),
    ("perplexity", r"pplx-[A-Za-z0-9]{32,}", ("pplx-",)),
    ("google-gemini", r"AIza[0-9A-Za-z_-]{35}", ("AIza",)),
    ("e2b", r"e2b_[a-f0-9]{32,}", ("e2b_",)),
    ("github", r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}", ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")),
    ("slack", r"xox[abprs]-[A-Za-z0-9-]{10,}", ("xoxa-", "xoxb-", "xoxp-", "xoxr-", "xoxs-")),
    ("aws-access-key", r"(?:AKIA|ASIA)[0-9A-Z]{16}", ("AKIA", "ASIA")),
    ("stripe", r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}", ("_live_", "_test_")),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}", ("hf_",)),
    ("groq", r"gsk_[A-Za-z0-9]{40,}", ("gsk_",)),
    ("tavily", r"tvly-[A-Za-z0-9_-]{20,}", ("tvly-",)),
    ("replicate", r"r8_[A-Za-z0-9]{30,}", ("r8_",)),
    ("notion", r"(?:ntn_|secret_)[A-Za-z0-9]{40,}", ("ntn_", "secret_")),
    ("sendgrid", r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", ("SG.",)),
    ("jwt", r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", ("eyJ",)),
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----", ("PRIVATE KEY",)),
    ("database-url", r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@\"']+:[^\s@\"']+@[^\s\"']+", ("://",)),
)

# Named assignments: NAME=value / NAME: value / "NAME": "value" where NAME holds a secret word and value looks random.
# The regex takes any identifier followed by an assignment and the secret word is checked afterwards; searching for the
# word inside the regex backtracked over every identifier and made the scan several times slower.
ASSIGNMENT_REGEX = re.compile(r"(?<![A-Za-z0-9_])(?P<name>[A-Za-z_][A-Za-z0-9_]{0,130})[\"']?\s*[=:]\s*[\"']?(?P<value>[A-Za-z0-9_\-./+=]{16,})")
SECRET_WORD = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|CLIENT_ID", re.IGNORECASE)
PLACEHOLDER_WORDS = ("example", "your", "placeholder", "changeme", "xxxx", "redacted", "dummy", "insert", "replace", "envh-proxy", "sk-proj-abc", "<", ">", "$", "%")
NAME_ONLY_PROVIDERS = ("serp", "fred", "asknews", "hyperbrowser", "metaculus", "exa", "mistral", "cohere", "together", "deepseek", "fireworks")

# Folders of installed code, caches and app data that hold no keys of yours but can hold millions of files. A name with
# a slash matches only that path below the folder being scanned (usually your home folder).
SKIP_DIR_NAMES = {
    "node_modules", ".venv", "venv", "env", "site-packages", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".pyenv", ".conda", "miniconda3", "anaconda3", "miniforge3", "mambaforge", ".local/lib", ".local/share/uv", ".local/share/pipx",
    ".local/share/virtualenvs", ".npm", ".yarn", ".pnpm-store", ".bun", ".nvm", ".cargo", ".rustup", ".gradle", ".m2", ".julia", ".gem",
    "go/pkg", ".terraform", ".vscode/extensions", ".vscode-insiders/extensions", ".cursor/extensions", ".vscode-server", ".cursor-server",
    ".cache", "Cache", "Code Cache", "GPUCache", "DawnCache", "CacheStorage", ".local/share/flatpak", ".local/share/containers",
    ".local/share/Steam", ".steam", ".wine", "snap", ".mozilla", "google-chrome", "chromium", "BraveSoftware", "Microsoft Edge",
    ".thunderbird", "Trash/expunged",
}
SKIPPED_SUMMARY = (
    "Skipped: installed packages and environments (.venv, node_modules, conda, uv's Pythons and more), caches (.cache, app",
    "caches, folders tagged with CACHEDIR.TAG), editor extensions, browser and mail profiles, Steam, Wine and snaps.",
    "The full list: envh scan --help",
)
CACHE_TAG = "CACHEDIR.TAG"
CACHE_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
KNOWN_KEY_FILES = (".cache/huggingface/token", ".cache/huggingface/stored_tokens")
GIT_INTERESTING = {"config", "COMMIT_EDITMSG", "FETCH_HEAD"}
ALWAYS_LOOK = (
    ".bash_history", ".zsh_history", ".python_history", ".node_repl_history", ".psql_history", ".mysql_history",
    ".netrc", ".npmrc", ".pypirc", ".gitconfig", ".git-credentials", ".profile", ".bashrc", ".zshrc", ".zshenv",
)
NOT_SCANNED_NOTE = (
    "Not scanned: git object history (use `git log -p -S<prefix>` in a repo), binary databases such as browser profiles and VS Code",
    "state (both can hold keys), encrypted stores such as the Claude desktop app's local environment, mounted drives outside the",
    "paths given, and anything over the size limit. Shell history and Claude Code transcripts under ~/.claude are included.",
)
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_WIDTH = 20
REDRAW_SECONDS = 0.1


@dataclass(frozen=True)
class Hit:
    path: Path
    line: int
    kind: str
    name: str
    prefix: str
    fingerprint: str
    length: int

    def to_json(self) -> dict[str, object]:
        return {"path": str(self.path), "line": self.line, "kind": self.kind, "name": self.name, "prefix": self.prefix, "fingerprint": self.fingerprint, "length": self.length}


def entropy(text: str) -> float:
    counts = Counter(text)
    total = len(text)
    return -sum(count / total * math.log2(count / total) for count in counts.values())


def looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(word in lowered for word in PLACEHOLDER_WORDS) or entropy(value) < 3.0


def redact(value: str) -> tuple[str, str]:
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return value[:4] + "\u2026", digest


def present_kinds(text: str) -> tuple[str, ...]:
    return tuple(kind for kind, _, markers in VALUE_PATTERNS if any(marker in text for marker in markers))


@functools.lru_cache(maxsize=None)
def value_regex(kinds: tuple[str, ...]) -> re.Pattern[str] | None:
    if not kinds:
        return None
    return re.compile("|".join(f"(?P<{kind.replace('-', '_')}>{pattern})" for kind, pattern, _ in VALUE_PATTERNS if kind in kinds))


def scan_text(path: Path, text: str) -> Iterator[Hit]:
    values = value_regex(present_kinds(text))
    for line_number, line in enumerate(text.splitlines(), start=1):
        seen_spans: list[tuple[int, int]] = []
        if values is not None:
            for match in values.finditer(line):
                value = match.group(0)
                kind = (match.lastgroup or "value").replace("_", "-")
                prefix, digest = redact(value)
                seen_spans.append(match.span())
                yield Hit(path, line_number, kind, "", prefix, digest, len(value))
        for match in ASSIGNMENT_REGEX.finditer(line):
            if SECRET_WORD.search(match.group("name"), 1) is None:
                continue
            value = match.group("value").rstrip(".,;")
            if looks_placeholder(value):
                continue
            if any(start <= match.start("value") < end for start, end in seen_spans):
                continue
            prefix, digest = redact(value)
            yield Hit(path, line_number, "named-assignment", match.group("name"), prefix, digest, len(value))


def read_if_text(path: Path) -> str | None:
    """The file's text, or None when it looks binary. Raises OSError when it cannot be read."""
    with path.open("rb") as handle:
        head = handle.read(8192)
        if b"\0" in head:
            return None
        return (head + handle.read()).decode("utf-8", errors="replace")


def tagged_as_cache(directory: Path) -> bool:
    try:
        with (directory / CACHE_TAG).open("rb") as handle:
            return handle.read(len(CACHE_TAG_SIGNATURE)) == CACHE_TAG_SIGNATURE
    except OSError:
        return False


def find_files(roots: list[Path], max_bytes: int, unreadable: list[Path], found: Callable[[int], None] | None = None) -> list[tuple[Path, int]]:
    """Every file to scan, with its size. Links are not followed, and the skipped folders are not entered."""
    files: list[tuple[Path, int]] = []

    def add(path: Path, always: bool = False) -> None:
        try:
            if path.is_symlink() or not path.is_file():
                return
            size = path.stat().st_size
        except OSError:
            return
        if size <= max_bytes or always:
            files.append((path, size))

    for root in roots:
        if root.is_file():
            files.append((root, root.stat().st_size))
            continue
        for known in KNOWN_KEY_FILES:
            add(root / known)
        for current, directories, names in os.walk(root, followlinks=False, onerror=lambda error: unreadable.append(Path(error.filename))):
            current_path = Path(current)
            kept: list[str] = []
            for directory in directories:
                full = current_path / directory
                if directory in SKIP_DIR_NAMES or str(full.relative_to(root)) in SKIP_DIR_NAMES or full.is_symlink() or tagged_as_cache(full):
                    continue
                if directory == ".git":
                    for name in GIT_INTERESTING:
                        add(full / name)
                    continue
                kept.append(directory)
            directories[:] = sorted(kept)
            for name in sorted(names):
                add(current_path / name, always=name in ALWAYS_LOOK)
            if found is not None:
                found(len(files))
    return files


def scan_files(files: list[tuple[Path, int]], unreadable: list[Path], progress: Progress | None = None) -> tuple[list[Hit], int]:
    hits: list[Hit] = []
    scanned = 0
    done = 0
    total = sum(size for _, size in files)
    for path, size in files:
        if progress is not None:
            progress.scanning(done, total, path.parent)
        done += size
        try:
            text = read_if_text(path)
        except OSError:
            unreadable.append(path)
            continue
        if text is None:
            continue
        scanned += 1
        hits.extend(scan_text(path, text))
    return hits, scanned


def scan_paths(roots: list[Path], max_bytes: int) -> tuple[list[Hit], int, list[Path]]:
    unreadable: list[Path] = []
    hits, scanned = scan_files(find_files(roots, max_bytes, unreadable), unreadable)
    return hits, scanned, unreadable


def readable_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s" if seconds >= 10 else f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60)}s"


def size_pair(done: int, total: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if total >= scale:
            return f"{done / scale:.1f} of {total / scale:.1f} {unit}"
    return f"{done} of {total} B"


def time_left(elapsed: float, fraction: float) -> str | None:
    """None until there is enough progress to estimate from."""
    if elapsed < 3 or fraction < 0.02:
        return None
    remaining = elapsed * (1 - fraction) / fraction
    return "<1 min left" if remaining < 60 else f"{round(remaining / 60)} min left"


def shown_folder(folder: Path) -> str:
    try:
        parts = folder.relative_to(Path.home()).parts
    except ValueError:
        return "/".join(folder.parts[:4])
    return "~/" + "/".join(parts[:3]) if parts else "~"


class Progress:
    """One line on a terminal, redrawn at most ten times a second: a file count while looking, then a bar by bytes."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.last_draw = -REDRAW_SECONDS
        self.frame = 0
        self.scan_started: float | None = None

    def finding(self, count: int) -> None:
        self._draw(f"{SPINNER[self.frame % len(SPINNER)]} Finding files… {count:,}")

    def scanning(self, done: int, total: int, folder: Path) -> None:
        now = time.monotonic()
        if self.scan_started is None:
            self.scan_started = now
        fraction = done / total if total else 1.0
        filled = int(fraction * BAR_WIDTH)
        left = time_left(now - self.scan_started, fraction)
        parts = [f"[{'█' * filled}{'░' * (BAR_WIDTH - filled)}] {fraction:.0%}", size_pair(done, total), *([left] if left else []), shown_folder(folder)]
        self._draw(" · ".join(parts))

    def clear(self) -> None:
        self.stream.write("\r\033[K")
        self.stream.flush()

    def _draw(self, text: str) -> None:
        now = time.monotonic()
        if now - self.last_draw < REDRAW_SECONDS:
            return
        self.last_draw = now
        self.frame += 1
        try:
            width = os.get_terminal_size(self.stream.fileno()).columns or 80
        except (OSError, ValueError):
            width = 80
        self.stream.write("\r\033[K  " + text[: max(width - 3, 10)])
        self.stream.flush()


def skipped_folders_help() -> str:
    return f"Skipped folders: {', '.join(sorted(SKIP_DIR_NAMES, key=str.lower))}, and any folder tagged with {CACHE_TAG}."


def report(hits: list[Hit], scanned: int, unreadable: list[Path], elapsed: float, say: Callable[[str], None] = print) -> None:
    by_path: dict[Path, list[Hit]] = defaultdict(list)
    for hit in hits:
        by_path[hit.path].append(hit)
    strings = f"{len(hits)} key-shaped string" + ("" if len(hits) == 1 else "s")
    files = f"{len(by_path)} file" + ("" if len(by_path) == 1 else "s")
    say(f"scanned {scanned:,} text files in {readable_duration(elapsed)}; {strings} in {files}")
    for path in sorted(by_path):
        say(f"\n{path}")
        for hit in sorted(by_path[path], key=lambda h: (h.line, h.kind)):
            label = hit.kind if not hit.name else f"{hit.kind} {hit.name}"
            say(f"  line {hit.line:<6} {label:<42} {hit.prefix:<6} {hit.length:>4} chars  id {hit.fingerprint}")
    by_fingerprint: dict[str, set[Path]] = defaultdict(set)
    for hit in hits:
        by_fingerprint[hit.fingerprint].add(hit.path)
    repeated = {digest: paths for digest, paths in by_fingerprint.items() if len(paths) > 1}
    if repeated:
        say("\nsame value found in several files (by id):")
        for digest, paths in sorted(repeated.items(), key=lambda item: -len(item[1])):
            say(f"  id {digest}: {len(paths)} files")
            for path in sorted(paths):
                say(f"      {path}")
    if unreadable:
        say(f"\n{len(unreadable)} files or directories could not be read (permissions); run as the owning user to check them")
    say("")
    for line in (*SKIPPED_SUMMARY, *NOT_SCANNED_NOTE):
        say(line)
    say(f"Keys without a distinctive format ({', '.join(NAME_ONLY_PROVIDERS)}) are only recognized inside named assignments such as FRED_API_KEY=...; a bare copy of one in a transcript cannot be identified reliably.")


def main(args: argparse.Namespace) -> int:
    roots = [Path(raw).expanduser() for raw in (args.paths or [str(Path.home())])]
    missing = [root for root in roots if not root.exists()]
    if missing:
        print(f"envh scan: no such path: {', '.join(str(path) for path in missing)}", file=sys.stderr)
        return 2
    progress = Progress(sys.stderr) if sys.stderr.isatty() else None
    started = time.monotonic()
    unreadable: list[Path] = []
    try:
        files = find_files(roots, args.max_size * 1024 * 1024, unreadable, progress.finding if progress is not None else None)
        hits, scanned = scan_files(files, unreadable, progress)
    finally:
        if progress is not None:
            progress.clear()
    if args.json:
        print(json.dumps({"scanned": scanned, "hits": [hit.to_json() for hit in hits], "unreadable": [str(path) for path in unreadable]}, indent=2))
    else:
        report(hits, scanned, unreadable, time.monotonic() - started)
    return 1 if hits else 0
