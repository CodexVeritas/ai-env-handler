"""`envh explore`: browse a saved scan, each key, the files it is in and the lines around each copy. Standard library only.

The scan's folder holds no values, so the lines around a key are read from the file when it is opened, with every key in
them shown as its first and last few characters. `s` shows the full key on the alternate screen, which leaves no
scrollback, and it hides itself again after REVEAL_SECONDS.
"""

from __future__ import annotations

import codecs
import itertools
import json
import math
import os
import re
import select
import sys
import termios
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from envh.client.transport import ClientError, Connection, waiting_notice
from envh.common import peek, printable
from envh.tools.saved_files import SCANS, saved_files_root, shown_path
from envh.tools.scanner import DEFAULT_MAX_BYTES, Hit, place_of, plural, read_if_text, scan_text, shown_folder

TIERS = ("high", "medium", "low", "fake", "all")
TIER_LABELS = {"high": "High", "medium": "Medium", "low": "Low", "fake": "Fake", "all": "All"}
GROUPINGS = ("type", "place")
VAULT_FILTERS = ("all keys", "in your vault", "not in your vault")
VAULT_FILE = "vault.json"
GROUP_ABOVE = 5
LINES_SHOWN_PER_FILE = 3
CONTEXT_LINES = 3
LEAD_CHARACTERS = 60
SLICE_CHARACTERS = 1000
REVEAL_SECONDS = 30
LONG_REST = 32
REVEALED_ROWS = 4
REDRAW_SECONDS = 0.5
TAB_WIDTH = 4
KEYS = {
    "\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down", "\x1b[C": "right", "\x1bOC": "right", "\x1b[D": "left", "\x1bOD": "left",
    "\x1b[5~": "page_up", "\x1b[6~": "page_down", "\x1b[H": "home", "\x1bOH": "home", "\x1b[1~": "home", "\x1b[F": "end", "\x1bOF": "end",
    "\x1b[4~": "end", "\x1b[Z": "shift_tab", "\t": "tab", "\r": "enter", "\n": "enter", "\x7f": "backspace", "\x08": "backspace", "\x03": "quit",
}
LONGEST_FIRST = sorted(KEYS, key=len, reverse=True)
OTHER_SEQUENCE = re.compile(r"\x1b(\[\[.|\[[0-?]*[ -/]*[@-~]|O.)", re.DOTALL)
PASTE_START, PASTE_END = "\x1b[200~", "\x1b[201~"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(frozen=True, slots=True)
class Found:
    """One hit as the scan saved it: where it is and how to tell it apart, never its value."""

    path: str
    line: int
    confidence: str
    kind: str
    name: str
    shown: str
    fingerprint: str
    length: int
    place: str

    def describe(self) -> str:
        return f"{self.shown} ({self.length} chars, id {self.fingerprint})"


@dataclass(frozen=True)
class Row:
    node: tuple[str, ...]
    parent: tuple[str, ...] | None
    depth: int
    label: str
    detail: str
    expandable: bool


@dataclass(frozen=True)
class ContextLine:
    """A line of the file view: its number (None for the gap between two stretches) and the keys on it."""

    number: int | None
    text: str = ""
    spans: tuple[tuple[int, int, str, bool], ...] = ()

    @property
    def holds_key(self) -> bool:
        return any(selected for _, _, _, selected in self.spans)


@dataclass
class FileView:
    path: str
    value: Found
    type_label: str
    lines: list[ContextLine] = field(default_factory=list)
    problem: str = ""
    top: int = 0
    revealed_until: float | None = None


def styled(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def cells(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def fit(segments: list[tuple[str, str]], width: int) -> str:
    """Join (text, style) segments into a row of at most width cells, ending in … when cut. Reads no further than the
    row needs, so a long line costs no more than a short one."""
    kept: list[tuple[str, str]] = []
    used = 0
    overflow = False
    for text, style in segments:
        for char in text:
            if used + cells(char) > width:
                overflow = True
                break
            kept.append((char, style))
            used += cells(char)
        if overflow:
            break
    if overflow:
        while kept and used > width - 1:
            used -= cells(kept.pop()[0])
        kept.append(("…", ""))
    pieces = []
    for style, group in itertools.groupby(kept, key=lambda item: item[1]):
        chunk = "".join(char for char, _ in group)
        pieces.append(styled(style, chunk) if style else chunk)
    return "".join(pieces)


def glimpse(value: str, fixed: int) -> str:
    """How the file view shows a key: like the report, but 8 characters at the start of a long key, enough to tell a
    placeholder such as 0123456789ab from a real key. Only you see it, on the alternate screen."""
    rest = value[fixed:]
    return value[:fixed] + (f"{rest[:8]}…{rest[-4:]}" if len(rest) >= LONG_REST else peek(rest))


ROW_HINTS = {"top": "→ shows its keys", "value": "→ shows the files it is in", "folder": "→ shows these files", "file": "Enter shows the lines around the key"}


def row_hint(row: Row, expanded: set[tuple[str, ...]]) -> str:
    return "← hides them" if row.node in expanded else ROW_HINTS[row.node[0]]


def safe(text: str) -> str:
    return printable(text.replace("\t", " " * TAB_WIDTH))


def split_keys(text: str) -> list[str]:
    """Key names (up, enter, tab, ...) or typed characters. A paste becomes its characters without its line breaks."""
    keys: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(PASTE_START, index):
            end = text.find(PASTE_END, index)
            end = len(text) if end < 0 else end
            keys.extend(char for char in text[index + len(PASTE_START):end] if char not in "\r\n")
            index = end + len(PASTE_END)
            continue
        known = next((sequence for sequence in LONGEST_FIRST if text.startswith(sequence, index)), None)
        if known is not None:
            keys.append(KEYS[known])
            index += len(known)
            continue
        other = OTHER_SEQUENCE.match(text, index)
        if other is not None:
            index = other.end()
            continue
        keys.append("escape" if text[index] == "\x1b" else text[index])
        index += 1
    return keys


def load_scan(folder: Path) -> list[Found]:
    try:
        data = json.loads((folder / "hits.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ClientError(f"can't read the scan in {shown_path(folder)}: {error}", 2) from error
    hits = data["hits"]
    if hits and "confidence" not in hits[0]:
        raise ClientError(f"{shown_path(folder)} was saved by an older envh scan; run envh scan again", 2)
    return [
        Found(hit["path"], hit["line"], hit["confidence"], hit["kind"], hit["name"], hit["shown"], hit["fingerprint"], hit["length"], place_of(hit["path"]))
        for hit in hits
    ]


def newest_scan(root: Path) -> Path:
    saved = sorted(root.glob("*/hits.json"))
    if not saved:
        raise ClientError("no saved scan yet; run envh scan first", 2)
    return saved[-1].parent


TypeKey = tuple[str, str]
Tree = dict[TypeKey, dict[str, dict[str, list[int]]]]


@dataclass
class Index:
    """The hits of one tier and filter, as type (or place) → value → file → line numbers, with each value's names."""

    tree: Tree
    first: dict[str, Found]
    names: dict[str, set[str]]
    grouping: str = "type"
    vault: dict[str, list[str]] = field(default_factory=dict)


def build_index(hits: list[Found], tier: str, needle: str, grouping: str = "type", vault: dict[str, list[str]] | None = None, vault_filter: str = "all keys") -> Index:
    vault = vault or {}
    tree: Tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    first: dict[str, Found] = {}
    names: dict[str, set[str]] = defaultdict(set)
    needle = needle.lower()
    for hit in hits:
        if tier != "all" and hit.confidence != tier:
            continue
        if vault_filter != "all keys" and (hit.fingerprint in vault) != (vault_filter == "in your vault"):
            continue
        stored = " ".join(vault.get(hit.fingerprint, ())).lower()
        if needle and not any(needle in field_text for field_text in (hit.kind.lower(), hit.name.lower(), hit.path.lower(), hit.place, stored)):
            continue
        tree[(hit.kind if grouping == "type" else hit.place, hit.confidence)][hit.fingerprint][hit.path].append(hit.line)
        first.setdefault(hit.fingerprint, hit)
        if hit.name:
            names[hit.fingerprint].add(hit.name)
    return Index(tree, first, names, grouping, vault)


def file_count(values: dict[str, dict[str, list[int]]]) -> int:
    return len({path for paths in values.values() for path in paths})


def lines_label(lines: list[int]) -> str:
    numbers = sorted(set(lines))
    shown = ", ".join(str(number) for number in numbers[:LINES_SHOWN_PER_FILE])
    more = f" and {len(numbers) - LINES_SHOWN_PER_FILE} more" if len(numbers) > LINES_SHOWN_PER_FILE else ""
    return ("line " if len(numbers) == 1 else "lines ") + shown + more


def tree_rows(index: Index, expanded: set[tuple[str, ...]], show_tier: bool) -> list[Row]:
    """The visible rows: every type, and below each expanded node its children. A key in more than GROUP_ABOVE files
    groups them by folder."""
    rows: list[Row] = []
    for (kind, confidence), values in sorted(index.tree.items(), key=lambda item: (-file_count(item[1]), item[0])):
        type_node = ("top", kind, confidence)
        detail = f"{plural(len(values), 'key')} in {plural(file_count(values), 'file')}"
        rows.append(Row(type_node, None, 0, kind, (f"{confidence} · " if show_tier else "") + detail, True))
        if type_node not in expanded:
            continue
        for fingerprint, paths in sorted(values.items(), key=lambda item: (-len(item[1]), item[0])):
            value_node = ("value", kind, confidence, fingerprint)
            names = sorted(index.names.get(fingerprint, ()))
            label = index.first[fingerprint].describe() + ("  " + ", ".join(names[:2]) + (" …" if len(names) > 2 else "") if names else "")
            if index.grouping == "place":
                label = f"{index.first[fingerprint].kind}  {label}"
            if fingerprint in index.vault:
                label += f"  · in your vault as {', '.join(index.vault[fingerprint])}"
            rows.append(Row(value_node, type_node, 1, label, plural(len(paths), "file"), True))
            if value_node not in expanded:
                continue
            if len(paths) <= GROUP_ABOVE:
                rows.extend(file_row(kind, confidence, fingerprint, path, lines, value_node, 2) for path, lines in sorted(paths.items()))
                continue
            folders: dict[str, list[str]] = defaultdict(list)
            for path in paths:
                folders[shown_folder(Path(path).parent)].append(path)
            for folder, members in sorted(folders.items(), key=lambda item: (-len(item[1]), item[0])):
                if len(members) == 1:
                    rows.append(file_row(kind, confidence, fingerprint, members[0], paths[members[0]], value_node, 2))
                    continue
                group_node = ("folder", kind, confidence, fingerprint, folder)
                rows.append(Row(group_node, value_node, 2, folder, plural(len(members), "file"), True))
                if group_node in expanded:
                    rows.extend(file_row(kind, confidence, fingerprint, path, paths[path], group_node, 3) for path in sorted(members))
    return rows


def file_row(kind: str, confidence: str, fingerprint: str, path: str, lines: list[int], parent: tuple[str, ...], depth: int) -> Row:
    return Row(("file", kind, confidence, fingerprint, path), parent, depth, shown_path(Path(path)), lines_label(lines), False)


def read_context(path: str, fingerprint: str) -> tuple[list[ContextLine], str]:
    """The stretches of the file around each copy of the key, read now; or a problem saying why there is nothing to show."""
    file = Path(path)
    try:
        if file.stat().st_size > DEFAULT_MAX_BYTES:
            return [], "This file is over 25 MB now, too large to show."
        text = read_if_text(file)
    except OSError as error:
        return [], f"Can't read this file: {error.strerror or error}."
    if text is None:
        return [], "This file isn't text any more."
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    by_line: dict[int, list[Hit]] = defaultdict(list)
    for hit in scan_text(file, text):
        by_line[hit.line].append(hit)
    copies = sorted(number for number, hits in by_line.items() if any(hit.fingerprint == fingerprint for hit in hits))
    if not copies:
        return [], "This key isn't in the file any more; it changed after the scan."
    wanted: list[int] = []
    for number in copies:
        for nearby in range(max(1, number - CONTEXT_LINES), min(len(lines), number + CONTEXT_LINES) + 1):
            if not wanted or nearby > wanted[-1]:
                wanted.append(nearby)
    shown: list[ContextLine] = []
    for number in wanted:
        if shown and shown[-1].number is not None and number > shown[-1].number + 1:
            shown.append(ContextLine(None))
        line = lines[number - 1]
        spans = tuple(sorted((hit.column, hit.column + hit.length, glimpse(line[hit.column:hit.column + hit.length], hit.fixed), hit.fingerprint == fingerprint) for hit in by_line.get(number, [])))
        shown.append(ContextLine(number, line, spans))
    return shown, ""


def context_segments(line: ContextLine, revealed: bool, room: int) -> list[tuple[str, str]]:
    """The line as styled segments, every key replaced by its glimpse, or the selected one by its value while revealed. A
    long line starts a little before the selected key, at most a third of the room, so the key is on screen."""
    focus = next((start for start, _, _, selected in line.spans if selected), None)
    begin = 0 if focus is None else max(0, focus - min(LEAD_CHARACTERS, room // 3))
    for start, end, _, _ in line.spans:
        if start < begin < end:
            begin = start
    segments: list[tuple[str, str]] = [("…", "2")] if begin else []
    cursor = begin
    for start, end, shown, selected in line.spans:
        if start < cursor:
            continue
        segments.append((safe(line.text[cursor:min(start, cursor + SLICE_CHARACTERS)]), ""))
        if selected:
            segments.append((safe(line.text[start:end]) if revealed else shown, "1;33"))
        else:
            segments.append((shown, "2"))
        cursor = end
    segments.append((safe(line.text[cursor:cursor + SLICE_CHARACTERS]), ""))
    return segments


class Explorer:
    """The state of the view and what each key does to it; render() returns the rows to draw."""

    def __init__(self, hits: list[Found], folder: Path, clock: Callable[[], float] = time.monotonic, vault: dict[str, list[str]] | None = None) -> None:
        self.hits = hits
        self.vault = vault
        self.vault_filter = "all keys"
        self.folder = folder
        self.clock = clock
        self.tier_counts = {tier: len({hit.fingerprint for hit in hits if tier == "all" or hit.confidence == tier}) for tier in TIERS}
        self.tier = next((tier for tier in TIERS if self.tier_counts[tier]), "all")
        self.needle = ""
        self.grouping = "type"
        self.editing_filter = False
        self.expanded: set[tuple[str, ...]] = set()
        self.selected = 0
        self.top = 0
        self.file_view: FileView | None = None
        self.message = ""
        self.done = False
        self._index_key: tuple[str, str, str, str] | None = None
        self._index: Index | None = None

    def index(self) -> Index:
        key = (self.tier, self.needle, self.grouping, self.vault_filter)
        if self._index_key != key or self._index is None:
            self._index_key, self._index = key, build_index(self.hits, self.tier, self.needle, self.grouping, self.vault, self.vault_filter)
        return self._index

    def rows(self) -> list[Row]:
        return tree_rows(self.index(), self.expanded, self.tier == "all")

    def handle(self, key: str, height: int) -> None:
        self.message = ""
        if key == "quit":
            self.done = True
        elif self.file_view is not None:
            self.handle_file_view(key, height)
        elif self.editing_filter:
            self.handle_filter(key)
        else:
            self.handle_tree(key, height)

    def handle_filter(self, key: str) -> None:
        if key == "enter":
            self.editing_filter = False
        elif key == "escape":
            self.editing_filter, self.needle = False, ""
        elif key == "backspace":
            self.needle = self.needle[:-1]
        elif len(key) == 1 and key.isprintable():
            self.needle += key
        self.selected = 0

    def handle_tree(self, key: str, height: int) -> None:
        rows = self.rows()
        row = rows[self.selected] if rows else None
        steps = {"up": -1, "down": 1, "page_up": -(height - 6), "page_down": height - 6, "home": -len(rows), "end": len(rows)}
        if key in steps and rows:
            self.selected = min(max(self.selected + steps[key], 0), len(rows) - 1)
        elif key in ("tab", "shift_tab"):
            self.tier = TIERS[(TIERS.index(self.tier) + (1 if key == "tab" else -1)) % len(TIERS)]
            self.selected, self.expanded = 0, set()
        elif key == "/":
            self.editing_filter = True
        elif key == "g":
            self.grouping = GROUPINGS[(GROUPINGS.index(self.grouping) + 1) % len(GROUPINGS)]
            self.selected, self.expanded = 0, set()
        elif key == "v" and self.vault is None:
            self.message = "Not checked against your vault. Reopen envh explore with the console running and answer y."
        elif key == "v":
            self.vault_filter = VAULT_FILTERS[(VAULT_FILTERS.index(self.vault_filter) + 1) % len(VAULT_FILTERS)]
            self.selected, self.expanded = 0, set()
        elif key in ("q", "escape"):
            if key == "escape" and self.needle:
                self.needle, self.selected = "", 0
            else:
                self.done = True
        elif row is None:
            return
        elif key == "right" and row.expandable and row.node in self.expanded:
            self.selected = min(self.selected + 1, len(rows) - 1)
        elif key in ("right", "enter") and row.expandable and row.node in self.expanded:
            self.expanded.discard(row.node)
        elif key in ("right", "enter") and row.expandable:
            self.expand(row.node)
        elif key == "enter" or (key == "right" and not row.expandable):
            self.open_file(row)
        elif key == "left" and row.node in self.expanded:
            self.expanded.discard(row.node)
        elif key == "left" and row.parent is not None:
            self.selected = next(index for index, candidate in enumerate(rows) if candidate.node == row.parent)

    def expand(self, node: tuple[str, ...]) -> None:
        """Expand the node, and its only child while it has just one, so a key in one file shows that file at once."""
        while True:
            self.expanded.add(node)
            children = [row for row in self.rows() if row.parent == node]
            if len(children) != 1 or not children[0].expandable:
                return
            node = children[0].node

    def open_file(self, row: Row) -> None:
        _, kind, confidence, fingerprint, path = row.node
        value = self.index().first[fingerprint]
        lines, problem = read_context(path, fingerprint)
        type_label = value.kind if self.tier != "all" else f"{value.kind} ({confidence})"
        self.file_view = FileView(path, value, type_label, lines, problem)
        self.jump(1, self.file_view, from_top=True)

    def handle_file_view(self, key: str, height: int) -> None:
        view = self.file_view
        assert view is not None
        page = max(height - 7, 1)
        steps = {"up": -1, "down": 1, "page_up": -page, "page_down": page, "home": -len(view.lines), "end": len(view.lines)}
        if key in steps:
            view.top = min(max(view.top + steps[key], 0), max(len(view.lines) - page, 0))
        elif key == "n":
            self.jump(1, view)
        elif key == "p":
            self.jump(-1, view)
        elif key == "s" and view.lines:
            view.revealed_until = None if view.revealed_until is not None else self.clock() + REVEAL_SECONDS
        elif key in ("escape", "left", "q", "backspace"):
            self.file_view = None

    def jump(self, direction: int, view: FileView, from_top: bool = False) -> None:
        """Scroll to the next (or previous) line holding the key, a few lines from the top."""
        copies = [index for index, line in enumerate(view.lines) if line.holds_key]
        if not copies:
            return
        current = view.top + CONTEXT_LINES
        if from_top:
            target = copies[0]
        elif direction > 0:
            target = next((index for index in copies if index > current), copies[0])
        else:
            target = next((index for index in reversed(copies) if index < current), copies[-1])
        view.top = max(target - CONTEXT_LINES, 0)

    def revealed_value(self) -> str:
        view = self.file_view
        assert view is not None
        line = next(line for line in view.lines if line.holds_key)
        start, end = next((start, end) for start, end, _, selected in line.spans if selected)
        return safe(line.text[start:end])

    def revealed(self) -> bool:
        view = self.file_view
        if view is None or view.revealed_until is None:
            return False
        if self.clock() >= view.revealed_until:
            view.revealed_until = None
            return False
        return True

    def render(self, width: int, height: int) -> list[str]:
        width = max(width - 1, 20)
        return self.render_file_view(width, height) if self.file_view is not None else self.render_tree(width, height)

    def header(self, width: int, crumbs: list[str]) -> list[str]:
        title = [(" envh explore", "1;36"), *[(f" › {crumb}", "1") for crumb in crumbs]]
        tabs: list[tuple[str, str]] = [(" ", "")]
        for tier in TIERS:
            label = f" {TIER_LABELS[tier]} {self.tier_counts[tier]:,} "
            tabs.append((label, "1;7" if tier == self.tier else "2"))
            tabs.append((" ", ""))
        tabs.append((f"  grouped by {self.grouping} (g)", "2"))
        tabs.append(("  vault not checked" if self.vault is None else f"  {self.vault_filter} (v)", "2"))
        if self.editing_filter or self.needle:
            tabs.append((f"  filter: {self.needle}" + ("▏" if self.editing_filter else ""), "36"))
        return [fit(title, width), fit(tabs, width), ""]

    def scan_label(self) -> str:
        try:
            return f"scan of {datetime.strptime(self.folder.name, '%Y%m%d-%H%M%S'):%Y-%m-%d %H:%M}"
        except ValueError:
            return shown_path(self.folder)

    def render_tree(self, width: int, height: int) -> list[str]:
        rows = self.rows()
        self.selected = min(self.selected, max(len(rows) - 1, 0))
        lines = self.header(width, [self.scan_label()])
        visible = max(height - 6, 1)
        if self.selected < self.top:
            self.top = self.selected
        elif self.selected >= self.top + visible:
            self.top = self.selected - visible + 1
        self.top = min(self.top, max(len(rows) - visible, 0))
        if not rows:
            lines.append("  No keys here. Tab switches between High, Medium, Low, Fake and All; Esc clears the filter.")
        for index in range(self.top, min(self.top + visible, len(rows))):
            row = rows[index]
            mark = ("▾ " if row.node in self.expanded else "▸ ") if row.expandable else "  "
            chosen = index == self.selected
            segments = [(" › " if chosen else "   ", "36"), ("    " * row.depth + mark, "2"), (row.label, "1" if chosen else ""), (f"   {row.detail}", "2")]
            if chosen:
                segments.append((f"   {row_hint(row, self.expanded)}", "36"))
            lines.append(fit(segments, width))
        lines = (lines + [""] * height)[: height - 2]
        if self.editing_filter:
            hint = "Type to filter by service, name, place or path · Enter keep · Esc clear"
        else:
            hint = "↑↓ move · → open · ← close · Enter view · Tab tier · g group · v vault · / filter · q quit"
        lines.append(fit([(f" {self.message}", "33")], width) if self.message else "")
        lines.append(fit([(f" {hint}", "2")], width))
        return lines

    def render_file_view(self, width: int, height: int) -> list[str]:
        view = self.file_view
        assert view is not None
        revealed = self.revealed()
        lines = self.header(width, [view.type_label, view.value.describe()])
        lines[2] = fit([(f" {shown_path(Path(view.path))}", "")], width)
        lines.append("")
        value = self.revealed_value() if revealed else ""
        wrapped = [value[start:start + width - 4] for start in range(0, len(value), width - 4)]
        shown_value = wrapped[:REVEALED_ROWS] if len(wrapped) <= REVEALED_ROWS else wrapped[:REVEALED_ROWS - 1] + [f"… and {len(value) - (REVEALED_ROWS - 1) * (width - 4):,} more characters"]
        page = max(height - 7 - len(shown_value), 1)
        if view.problem:
            lines.append(fit([(f"  ! {view.problem}", "33")], width))
        gutter = len(str(max((line.number or 0 for line in view.lines), default=0)))
        for line in view.lines[view.top:view.top + page]:
            if line.number is None:
                lines.append(fit([(" " * (gutter + 4) + "┄", "2")], width))
                continue
            mark = (" › ", "33") if line.holds_key else ("   ", "")
            lines.append(fit([mark, (f"{line.number:>{gutter}} │ ", "2"), *context_segments(line, revealed, width - gutter - 6)], width))
        lines = (lines + [""] * height)[: height - 2 - len(shown_value)]
        lines.extend(fit([(f"    {chunk}", "1;33")], width) for chunk in shown_value)
        if revealed and view.revealed_until is not None:
            seconds = max(math.ceil(view.revealed_until - self.clock()), 1)
            lines.append(fit([(f" ! The key is showing, above and in its line. It hides in {seconds}s, or press s.", "33")], width))
        else:
            copies = sum(line.holds_key for line in view.lines)
            holding = "1 line here holds it" if copies == 1 else f"{copies} lines here hold it; n and p jump between them"
            lines.append(fit([(" Press s to show the full key.", "1;33"), (f" {holding}. Other keys show dimmed.", "2")], width) if copies else "")
        lines.append(fit([(" ↑↓ scroll · n next copy · p previous · s show the key · Esc back", "2")], width))
        return lines

    def run(self, terminal: Terminal) -> None:
        shown: list[str] = []
        size: tuple[int, int] | None = None
        while not self.done:
            width, height = terminal.size()
            if (width, height) != size:
                size, shown = (width, height), []
                terminal.write("\x1b[2J")
            shown = terminal.draw(self.render(width, height), shown, height)
            text = terminal.read(REDRAW_SECONDS)
            for key in split_keys(text) if text else []:
                self.handle(key, height)
                if self.done:
                    break


class Terminal:
    """The real terminal: raw keys on the alternate screen, drawn by rewriting only the rows that changed."""

    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def size(self) -> tuple[int, int]:
        size = os.get_terminal_size(self.descriptor)
        return size.columns, size.lines

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def read(self, timeout: float) -> str | None:
        if not select.select([self.descriptor], [], [], timeout)[0]:
            return None
        return self.decoder.decode(os.read(self.descriptor, 4096))

    def draw(self, lines: list[str], shown: list[str], height: int) -> list[str]:
        rows = (lines + [""] * height)[:height]
        changed = [f"\x1b[{number};1H\x1b[2K{row}" for number, row in enumerate(rows, start=1) if number > len(shown) or shown[number - 1] != row]
        if changed:
            self.write("".join(changed))
        return rows


@contextmanager
def full_screen(descriptor: int) -> Iterator[None]:
    """Raw keys on the alternate screen, put back on exit, quietly if the terminal has gone away."""
    saved = termios.tcgetattr(descriptor)
    raw = list(saved)
    raw[0] = raw[0] & ~termios.IXON
    raw[3] = raw[3] & ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
    raw[6] = list(saved[6])
    raw[6][termios.VMIN] = 1
    raw[6][termios.VTIME] = 0
    termios.tcsetattr(descriptor, termios.TCSADRAIN, raw)
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?2004h")
    sys.stdout.flush()
    try:
        yield
    finally:
        try:
            sys.stdout.write("\x1b[?2004l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        except (OSError, termios.error):
            pass


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def load_vault_matches(folder: Path) -> dict[str, list[str]] | None:
    try:
        return json.loads((folder / VAULT_FILE).read_text(encoding="utf-8"))["matches"]
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError) as error:
        raise ClientError(f"can't read {shown_path(folder / VAULT_FILE)}: {error}; delete it to check again", 2) from error


def match_against_vault(folder: Path, hits: list[Found], sock: Path) -> dict[str, list[str]]:
    """Ask the broker which found values are stored keys (the console approves it), and save the answer, names and ids
    only, beside the scan."""
    with Connection(sock) as conn:
        conn.send(op="match", ids=sorted({hit.fingerprint for hit in hits}), reason="envh explore: mark which keys envh scan found are in your vault")
        reply = conn.recv_ok()
        with waiting_notice(reply, None):
            reply = conn.recv_ok()
    matches: dict[str, list[str]] = reply.get("matches") or {}
    with os.fdopen(os.open(folder / VAULT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump({"matches": matches, "checked": datetime.now().isoformat(timespec="seconds")}, handle, indent=2)
    return matches


def vault_for(folder: Path, hits: list[Found], sock: Path) -> dict[str, list[str]] | None:
    """The saved vault matches, or a fresh check if the person agrees; None when the scan stays unchecked."""
    saved = load_vault_matches(folder)
    if saved is not None:
        return saved
    print("")
    print("Your envh vault can tell which of these keys are yours: the explorer then marks each one")
    print('"in your vault as NAME", so you know which copies are your current keys.')
    print("Only a short fingerprint of each key is sent, never the key. You approve it once on the envh console.")
    print("")
    answer = input("Check these keys against your vault? [Y/n] ").strip().lower()
    if answer in ("n", "no"):
        return None
    try:
        matches = match_against_vault(folder, hits, sock)
    except ClientError as error:
        print(f"Not checked against your vault: {error}", file=sys.stderr)
        return None
    stored = len({name for names in matches.values() for name in names})
    print(f"{plural(len(matches), 'found key')} {'is' if len(matches) == 1 else 'are'} in your vault ({plural(stored, 'stored key')}).")
    return matches


def explore(folder: Path, sock: Path) -> int:
    if not interactive():
        raise ClientError("envh explore is an interactive view; run it in a terminal", 2)
    print(f"Opening the scan in {shown_path(folder)}…", flush=True)
    hits = load_scan(folder)
    vault = vault_for(folder, hits, sock)
    with full_screen(sys.stdin.fileno()):
        Explorer(hits, folder, vault=vault).run(Terminal(sys.stdin.fileno()))
    return 0


def main(folder: str | None, sock: Path) -> int:
    return explore(Path(folder).expanduser().absolute() if folder else newest_scan(saved_files_root() / SCANS), sock)
