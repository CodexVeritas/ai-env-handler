"""The visible folder in your home where envh saves .env backups and scan results, so they are easy to find and delete."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

FOLDER_NAME = "envh-backups-and-scans"
IMPORT_BACKUPS = "import-backups"
SCANS = "scans"
README_NAME = "README.txt"
VAULT_FILE = "vault.json"
README_TEXT = f"""\
envh saves two kinds of files here. Each save gets its own folder, named by its date and time.

{IMPORT_BACKUPS}/  Your .env files as they were before `envh import` rewrote them. They hold your old keys in
                plaintext, readable by anything running as you. Once the rewritten projects work, delete them:
                rm -r ~/{FOLDER_NAME}/{IMPORT_BACKUPS}
{SCANS}/           Results of `envh scan`: where key-shaped strings are, never the keys themselves. Start with the
                summary at the top of report.txt, or browse them with `envh explore`. Nothing secret, so delete them
                whenever you like.

envh does not need anything in this folder. Deleting it loses only these backups and reports.
"""


def saved_files_root() -> Path:
    return Path.home() / FOLDER_NAME


def shown_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def ask_where(what: str, default: Path, ask: Callable[[str], str]) -> Path:
    answer = ask(f"Save {what} in a new dated folder inside {shown_path(default)}? Enter to accept, or type another folder: ").strip()
    return Path(answer).expanduser().absolute() if answer else default


def save_vault_matches(folder: Path, matches: dict[str, list[str]]) -> None:
    """Which found values are stored keys, as the broker answered: ids and names only, beside the scan."""
    with os.fdopen(os.open(folder / VAULT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump({"matches": matches, "checked": datetime.now().isoformat(timespec="seconds")}, handle, indent=2)


def new_dated_folder(parent: Path, stamp: str) -> Path:
    """parent/stamp, which only its owner can open. Saving into the shared folder also leaves its README there."""
    root = saved_files_root()
    if parent.parent == root:
        root.mkdir(mode=0o700, exist_ok=True)
        readme = root / README_NAME
        if not readme.exists():
            readme.write_text(README_TEXT, encoding="utf-8")
    folder = parent / stamp
    folder.mkdir(mode=0o700, parents=True)
    return folder


def styled(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() and "NO_COLOR" not in os.environ else text


def say_where_saved(folder: Path, holds_keys: bool, say: Callable[[str], None]) -> None:
    """The closing lines of an import or a scan, set apart so they are not missed: where its files are, and how to delete them."""
    shown = shown_path(folder)
    say("")
    if holds_keys:
        say(f"  {styled('33', '!')} {styled('1', 'Your old keys are still in the backup, in plaintext:')}")
        say(f"      {shown}")
        say("    Once the project works, delete it:")
        say(f"      {styled('1', f'rm -r {shown}')}")
    else:
        say(f"  {styled('32', '✓')} {styled('1', f'Results saved in {shown}')}")
        say("    Open report.txt; its summary is at the top. files.txt lists just the paths.")
        say(f"    They hold no keys. Delete them when you are done: rm -r {shown}")
