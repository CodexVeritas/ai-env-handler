"""Changes made outside the console's screens: editing config.yaml or presets.yaml in a text editor, and changing the
vault passphrase. Both ask for the vault passphrase first."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from envh.core.config import CONFIG_FILE, PRESETS_FILE, ConfigError, config_from_text, dump_presets, parse_presets, parse_settings
from envh.core.vault import MIN_PASSPHRASE_LENGTH, write_private_file
from envh.server.console.dialogs import Choice, Prompt
from envh.server.console.summaries import preset_changes, preset_drafts, settings_changes
from envh.server.console.tui import DIM, Line, styled

if TYPE_CHECKING:
    from envh.server.console.app import ConsoleApp


def pick_editor() -> str | None:
    for candidate in (os.environ.get("VISUAL"), os.environ.get("EDITOR"), "nano", "vi"):
        if candidate and shutil.which(candidate):
            return candidate
    return None


def start_edit(app: ConsoleApp, which: str) -> None:
    editor = pick_editor()
    if editor is None:
        app.tell("No text editor found. Install nano, or set $EDITOR for the envh user.", "warn")
        return
    path = app.broker.data_dir / (CONFIG_FILE if which == "config" else PRESETS_FILE)
    details = [styled("What you save is checked, and shown for a last look, before it is used.")]
    app.ask_passphrase(f"Open {path.name} in {editor}", details, lambda: edit(app, which, editor, path, path.read_text()))


def edit(app: ConsoleApp, which: str, editor: str, path: Path, text: str) -> None:
    original = path.read_text()
    scratch = path.with_name(path.name + ".edit")
    write_private_file(scratch, text.encode())
    try:
        app.suspend(lambda: subprocess.run([editor, str(scratch)], check=False))
        edited = scratch.read_text()
    finally:
        scratch.unlink(missing_ok=True)
    if edited == original:
        app.tell("No changes.")
        return
    again = ("Edit it again", lambda: app.attempt(lambda: edit(app, which, editor, path, edited)))
    drop = ("Throw my edits away", lambda: app.tell("Nothing changed."))
    try:
        changes = check(app, which, edited)
    except ConfigError as error:
        app.open(Choice(app, f"{path.name} has a problem", [again, drop], lines=[styled(str(error))]))
        return
    save = ("Save it", lambda: app.attempt(lambda: save_file(app, which, editor, path, edited)))
    app.open(Choice(app, f"Save {path.name}?", [save, again, drop], lines=changes or [styled("Only comments or layout changed.", DIM)]))


def check(app: ConsoleApp, which: str, text: str) -> list[Line]:
    """What saving text as the file would change; raises ConfigError when the broker could not load it."""
    broker = app.broker
    if which == "config":
        config_from_text(text, dump_presets(broker.config.presets_raw), set(broker.vault.names()))
        return settings_changes(broker.config.settings, parse_settings(text))
    _, raw = parse_presets(text, set(broker.vault.names()))
    return preset_changes(preset_drafts(broker.config.presets_raw), preset_drafts(raw))


def save_file(app: ConsoleApp, which: str, editor: str, path: Path, text: str) -> None:
    check(app, which, text)
    write_private_file(path, text.encode())
    app.broker.audit.event("config_edited", file=path.name, editor=editor)
    app.broker.reload()
    app.changed()
    app.tell(f"Saved {path.name}.", "ok")


def start_passphrase_change(app: ConsoleApp) -> None:
    details = [styled("Then type the new passphrase twice. The vault is encrypted again with it.")]
    app.ask_passphrase("Change the vault passphrase", details, lambda: app.open(Prompt(app, "New vault passphrase", lambda first: _repeat(app, first), hidden=True, phrase=True, hint=f"At least {MIN_PASSPHRASE_LENGTH} characters.")))


def _repeat(app: ConsoleApp, first: str) -> None:
    if len(first) < MIN_PASSPHRASE_LENGTH:
        app.tell(f"Use at least {MIN_PASSPHRASE_LENGTH} characters. The passphrase is unchanged.", "warn")
        return
    app.open(Prompt(app, "Repeat the new passphrase", lambda second: _change(app, first, second), hidden=True, phrase=True))


def _change(app: ConsoleApp, first: str, second: str) -> None:
    if second != first:
        app.tell("They don't match. The passphrase is unchanged.", "warn")
        return
    app.tell("Encrypting the vault with the new passphrase…")
    app.redraw()
    if app.attempt(lambda: app.broker.change_passphrase(first)):
        app.tell("Changed the vault passphrase. Use the new one from now on.", "ok")
