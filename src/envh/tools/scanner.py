"""`envh scan`: find where key-shaped strings live on this machine, without ever printing a value. Standard library only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Provider formats that are distinctive enough to recognize anywhere, including transcripts and shell history.
VALUE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("openrouter", r"sk-or-v1-[a-f0-9]{64}"),
    ("anthropic", r"sk-ant-[A-Za-z0-9_-]{32,}"),
    ("openai", r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{32,}"),
    ("openai-legacy", r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"),
    ("perplexity", r"pplx-[A-Za-z0-9]{32,}"),
    ("google-gemini", r"AIza[0-9A-Za-z_-]{35}"),
    ("e2b", r"e2b_[a-f0-9]{32,}"),
    ("github", r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}"),
    ("slack", r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    ("aws-access-key", r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    ("stripe", r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),
    ("huggingface", r"hf_[A-Za-z0-9]{30,}"),
    ("groq", r"gsk_[A-Za-z0-9]{40,}"),
    ("tavily", r"tvly-[A-Za-z0-9_-]{20,}"),
    ("replicate", r"r8_[A-Za-z0-9]{30,}"),
    ("notion", r"(?:ntn_|secret_)[A-Za-z0-9]{40,}"),
    ("sendgrid", r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    ("jwt", r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("private-key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    ("database-url", r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@\"']+:[^\s@\"']+@[^\s\"']+"),
)
VALUE_REGEX = re.compile("|".join(f"(?P<{name.replace('-', '_')}>{pattern})" for name, pattern in VALUE_PATTERNS))

# Named assignments: NAME=value / NAME: value / "NAME": "value" where NAME looks like a secret and value looks random.
ASSIGNMENT_REGEX = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|API_KEY|APIKEY|ACCESS_KEY|CLIENT_ID|CLIENT_SECRET)[A-Za-z0-9_]*)"
    r"[\"']?\s*[=:]\s*[\"']?(?P<value>[A-Za-z0-9_\-./+=]{16,})",
    re.IGNORECASE,
)
PLACEHOLDER_WORDS = ("example", "your", "placeholder", "changeme", "xxxx", "redacted", "dummy", "insert", "replace", "envh-proxy", "sk-proj-abc", "<", ">", "$", "%")
NAME_ONLY_PROVIDERS = ("serp", "fred", "asknews", "hyperbrowser", "metaculus", "exa", "mistral", "cohere", "together", "deepseek", "fireworks")

SKIP_DIR_NAMES = {
    "node_modules", ".venv", "venv", "env", "site-packages", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".cache", ".npm", ".cargo", ".rustup", ".nvm", ".gradle", ".m2", ".pyenv", ".local/lib", ".vscode-server", ".steam", "snap",
    ".mozilla", "google-chrome", "chromium", "BraveSoftware", "Microsoft Edge", ".thunderbird", ".wine", "Trash/expunged",
}
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


def scan_text(path: Path, text: str) -> Iterator[Hit]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 200_000:
            line = line[:200_000]
        seen_spans: list[tuple[int, int]] = []
        for match in VALUE_REGEX.finditer(line):
            value = match.group(0)
            kind = (match.lastgroup or "value").replace("_", "-")
            prefix, digest = redact(value)
            seen_spans.append(match.span())
            yield Hit(path, line_number, kind, "", prefix, digest, len(value))
        for match in ASSIGNMENT_REGEX.finditer(line):
            value = match.group("value").rstrip(".,;")
            if looks_placeholder(value):
                continue
            if any(start <= match.start("value") < end for start, end in seen_spans):
                continue
            prefix, digest = redact(value)
            yield Hit(path, line_number, "named-assignment", match.group("name"), prefix, digest, len(value))


def is_probably_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return False
    return b"\0" not in head


def iter_files(roots: list[Path], max_bytes: int) -> Iterator[Path]:
    for root in roots:
        if root.is_file():
            yield root
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            kept: list[str] = []
            for directory in directories:
                full = current_path / directory
                if directory in SKIP_DIR_NAMES or str(full.relative_to(root)) in SKIP_DIR_NAMES or full.is_symlink():
                    continue
                if directory == ".git":
                    for name in GIT_INTERESTING:
                        candidate = full / name
                        if candidate.is_file():
                            yield candidate
                    continue
                kept.append(directory)
            directories[:] = sorted(kept)
            for name in sorted(files):
                path = current_path / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_size > max_bytes and name not in ALWAYS_LOOK:
                        continue
                except OSError:
                    continue
                yield path


def scan_paths(roots: list[Path], max_bytes: int) -> tuple[list[Hit], int, list[Path]]:
    hits: list[Hit] = []
    scanned = 0
    unreadable: list[Path] = []
    for path in iter_files(roots, max_bytes):
        if not is_probably_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unreadable.append(path)
            continue
        scanned += 1
        hits.extend(scan_text(path, text))
    return hits, scanned, unreadable


def report(hits: list[Hit], scanned: int, unreadable: list[Path], say=print) -> None:
    by_path: dict[Path, list[Hit]] = defaultdict(list)
    for hit in hits:
        by_path[hit.path].append(hit)
    say(f"scanned {scanned} text files; {len(hits)} key-shaped strings in {len(by_path)} files")
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
        say(f"\n{len(unreadable)} files could not be read (permissions); run as the owning user to check them")
    say("")
    for line in NOT_SCANNED_NOTE:
        say(line)
    say(f"Keys without a distinctive format ({', '.join(NAME_ONLY_PROVIDERS)}) are only recognized inside named assignments such as FRED_API_KEY=...; a bare copy of one in a transcript cannot be identified reliably.")


def main(args: argparse.Namespace) -> int:
    roots = [Path(raw).expanduser() for raw in (args.paths or [str(Path.home())])]
    missing = [root for root in roots if not root.exists()]
    if missing:
        print(f"envh scan: no such path: {', '.join(str(path) for path in missing)}", file=sys.stderr)
        return 2
    hits, scanned, unreadable = scan_paths(roots, args.max_size * 1024 * 1024)
    if args.json:
        print(json.dumps({"scanned": scanned, "hits": [hit.to_json() for hit in hits], "unreadable": [str(path) for path in unreadable]}, indent=2))
    else:
        report(hits, scanned, unreadable)
    return 1 if hits else 0
