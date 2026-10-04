import asyncio
import os
import stat
from pathlib import Path

import pytest

from envh.core.state import Provenance, Request
from envh.core.vault import Vault
from envh.server import console as console_module
from envh.server.console import Console, ConsoleOutput, parse_line, render_request
from tests.conftest import ME, PASSPHRASE, PASSPHRASE_LINE, Harness

PHRASE = "amber basil cedar"


def test_parse_line_table() -> None:
    assert parse_line("  ").kind == "empty"
    assert parse_line("add KEY").command == "add" and parse_line("add KEY").args == ("KEY",)
    assert parse_line("4821").kind == "command"


async def test_render_request_shows_reason_and_the_passphrase_prompt(harness: Harness) -> None:
    mapping, preset = harness.broker.mapping_from_preset("dbwork")
    request = harness.broker.request_session(mapping, preset, 30, None, ("envh", "session", "start", "dbwork"), harness.provenance())
    text = "\n".join(render_request(request, harness.clock()))
    assert "no reason given" in text
    assert "OPENAI_API_KEY" in text and "excluded" in text and "DATABASE_URL" in text
    assert "sk-openai" not in text


def new_console(harness: Harness) -> tuple[Console, asyncio.StreamReader, list[str]]:
    reader = asyncio.StreamReader()
    said: list[str] = []
    return Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE), reader, said


def run_request(harness: Harness, reason: str | None = "why") -> Request:
    return harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, None, reason, ("python", "x.py"), harness.provenance())


async def test_requests_are_shown_one_at_a_time_and_approved_with_the_passphrase(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    first = run_request(harness)
    second = run_request(harness)
    assert console.current is first
    assert said[-1] == f"   [{PHRASE}] vault passphrase to approve #{first.id} (hidden), or n to deny:"
    assert not any(f"REQUEST #{second.id}" in line for line in said)
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=5)
    assert first.decision.result().outcome == "approved"
    assert second.decision.result().outcome == "denied"
    approved_at = said.index(f"approved #{first.id}")
    assert any(f"REQUEST #{second.id}" in line for line in said[approved_at:])
    assert console.quit_requested.is_set()
    assert not any(PASSPHRASE in line for line in said + harness.echoed)


async def test_wrong_passphrase_neither_approves_nor_changes_anything(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "WRONG_PASSPHRASE_PAUSE_SECONDS", 0)
    console, reader, said = new_console(harness)
    request = run_request(harness)
    await console.handle_line("guess-one\n")
    assert request.pending and console.current is request
    assert any(f"#{request.id} is still waiting" in line for line in said)
    await console.handle_line("n\n")
    reader.feed_data(b"guess-two\n")
    await console.handle_line("add NEW_SECRET")
    assert "NEW_SECRET" not in harness.broker.vault
    assert sum("wrong_passphrase" in line for line in harness.echoed) == 2
    assert not any("guess-" in line for line in said + harness.echoed)


async def test_commands_typed_while_a_request_waits_are_taken_as_answers(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "WRONG_PASSPHRASE_PAUSE_SECONDS", 0)
    console, _, said = new_console(harness)
    request = run_request(harness)
    await console.handle_line("quit\n")
    assert not console.quit_requested.is_set()
    assert request.pending
    assert not any("quit" in line for line in said)


async def test_a_withdrawn_request_discards_the_next_line_unread(harness: Harness) -> None:
    console, _, said = new_console(harness)
    withdrawn = run_request(harness)
    harness.broker.state.withdraw(withdrawn)
    await asyncio.sleep(0)
    assert console.current is None
    assert any(f"#{withdrawn.id} was withdrawn" in line for line in said)
    waiting = run_request(harness)
    await console.handle_line(PASSPHRASE_LINE.decode())
    assert waiting.pending
    assert "input discarded" in said
    console._advance()
    assert console.current is waiting
    await console.handle_line(PASSPHRASE_LINE.decode())
    assert waiting.decision.result().outcome == "approved"


async def test_console_admin_add_rm_reload_quit(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    reader.feed_data(b"add NEW_SECRET\n")
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"super-secret-value\n")
    reader.feed_data(b"y\n")
    reader.feed_data(b"rm NEW_SECRET\n")
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"y\n")
    reader.feed_data(b"secrets\n")
    reader.feed_data(b"presets\n")
    reader.feed_data(b"reload\n")
    reader.feed_data(b"bogus\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=5)
    assert console.quit_requested.is_set()
    assert "NEW_SECRET" not in harness.broker.vault
    assert any("admin_add" in line for line in harness.echoed)
    assert any("admin_rm" in line for line in harness.echoed)
    assert any("team" in line for line in said)
    assert any("unknown command" in line for line in said)
    assert not any("super-secret-value" in line for line in said)


async def test_requests_arriving_while_busy_are_shown_after(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    reader.feed_data(b"add LATER_KEY\n")
    task = asyncio.create_task(console.run())
    await asyncio.sleep(0.05)
    request = run_request(harness, reason=None)
    assert not any(f"REQUEST #{request.id}" in line for line in said)
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"value\n")
    reader.feed_data(b"n\n")
    await asyncio.sleep(0.05)
    assert any(f"REQUEST #{request.id}" in line for line in said)
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(task, timeout=5)
    assert request.decision.result().outcome == "denied"


async def test_edit_presets_validates_before_saving(harness: Harness, tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    bad_editor = tmp_path / "bad_editor.sh"
    bad_editor.write_text("#!/bin/sh\nprintf 'presets: {broken: {env: {X: NOPE}}}\\n' > \"$1\"\n")
    good_editor = tmp_path / "good_editor.sh"
    good_editor.write_text("#!/bin/sh\nprintf 'presets: {edited: {max_session: 2h, env: {X: OPENAI_API_KEY}}}\\n' > \"$1\"\n")
    for script in (bad_editor, good_editor):
        os.chmod(script, stat.S_IRWXU)
    presets_path = harness.data_dir / "presets.yaml"
    before = presets_path.read_text()
    reader.feed_data(f"edit presets {bad_editor}\n".encode())
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"n\n")
    reader.feed_data(f"edit presets {good_editor}\n".encode())
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"y\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert any("rejected" in line and "not in the vault" in line for line in said)
    assert "edited" in harness.broker.config.presets
    assert "team" not in presets_path.read_text() and before != presets_path.read_text()
    assert not (harness.data_dir / "presets.yaml.edit").exists()
    assert any("config_edited" in line for line in harness.echoed)


async def test_edit_config_discard_keeps_file(harness: Harness, tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    editor = tmp_path / "editor.sh"
    editor.write_text(f"#!/bin/sh\nprintf 'users: [{ME}]\\ndefaults: {{approval: per-run}}\\n' > \"$1\"\n")
    os.chmod(editor, stat.S_IRWXU)
    config_path = harness.data_dir / "config.yaml"
    before = config_path.read_text()
    reader.feed_data(f"edit config {editor}\n".encode())
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert config_path.read_text() == before
    assert harness.broker.config.defaults.approval == "session"
    assert any("discarded" in line for line in said)


async def test_unknown_editor_and_unexpected_errors_keep_console_alive(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    reader.feed_data(b"edit presets definitely-not-an-editor\n")
    reader.feed_data(b"help\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert any("not found" in line for line in said)
    assert any("commands:" in line for line in said)


async def test_render_request_neutralizes_control_characters(harness: Harness) -> None:
    reason = "backfill\n   OPENAI_API_KEY <- OPENAI_API_KEY\x1b[1A\x1b[2K"
    provenance = Provenance(pid=1, uid=harness.provenance().uid, cmdline="envh\x1b[2J session start")
    request = harness.broker.request_run({"DATABASE_URL": "DATABASE_URL"}, None, reason, ("envh", "run\r--with"), provenance)
    lines = render_request(request, harness.clock())
    assert len(lines) == len("\n".join(lines).splitlines())
    assert not any("\x1b" in line or "\r" in line for line in lines)
    assert lines[0].startswith("\a") and "DATABASE_URL" in "\n".join(lines)
    assert not any("\x1b" in line or "\n" in line for line in harness.echoed)


async def test_edit_config_without_users_is_rejected(harness: Harness, tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    editor = tmp_path / "editor.sh"
    editor.write_text("#!/bin/sh\nprintf 'defaults: {approval: per-run}\\n' > \"$1\"\n")
    os.chmod(editor, stat.S_IRWXU)
    config_path = harness.data_dir / "config.yaml"
    before = config_path.read_text()
    reader.feed_data(f"edit config {editor}\n".encode())
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert any("rejected" in line and "users is empty" in line for line in said)
    assert config_path.read_text() == before


async def test_add_is_audited_even_when_reload_fails(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    (harness.data_dir / "presets.yaml").write_text("presets: {broken: {env: {X: NOPE}}}\n")
    reader.feed_data(b"add NEW_ONE\n")
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"value-one\n")
    reader.feed_data(b"y\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=5)
    assert "NEW_ONE" in harness.broker.vault
    assert any("admin_add" in line and "NEW_ONE" in line for line in harness.echoed)
    assert any(line.startswith("error:") and "not in the vault" in line for line in said)


async def test_passphrase_change_reencrypts_the_vault_and_takes_effect(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "WRONG_PASSPHRASE_PAUSE_SECONDS", 0)
    console, reader, said = new_console(harness)
    reader.feed_data(PASSPHRASE_LINE)
    reader.feed_data(b"new-vault-passphrase\n")
    reader.feed_data(b"new-vault-passphrase\n")
    await console.handle_line("passphrase")
    assert Vault.open(harness.data_dir / "vault.age", "new-vault-passphrase").names() == harness.broker.vault.names()
    assert any("vault_passphrase_changed" in line for line in harness.echoed)
    request = run_request(harness)
    await console.handle_line(PASSPHRASE_LINE.decode())
    assert request.pending
    await console.handle_line("new-vault-passphrase\n")
    assert request.decision.result().outcome == "approved"


async def test_rm_refuses_a_secret_that_presets_use(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE)
    reader.feed_data(b"rm TEAM_OPENROUTER_KEY\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=5)
    assert "TEAM_OPENROUTER_KEY" in harness.broker.vault
    assert any(line.startswith("error:") and "used by presets team" in line for line in said)
    assert not any("admin_rm" in line for line in harness.echoed)


async def test_render_import_shows_reused_and_renamed_names(harness: Harness) -> None:
    secrets = {"OPENAI_API_KEY": "sk-other-project", "SAME_KEY": "sk-openai", "FRESH_KEY": "fresh-value"}
    request = harness.broker.request_import(secrets, {}, None, harness.provenance())
    text = "\n".join(render_request(request, harness.clock()))
    assert "stored as OPENAI_API_KEY_2: the vault's OPENAI_API_KEY holds a different value" in text
    assert "same value already stored as OPENAI_API_KEY" in text
    assert "added     FRESH_KEY" in text
    assert not any(value in text for value in secrets.values())


async def test_keys_view_renames_with_f2_and_the_passphrase(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    reader.feed_data(b"\x1b[B\x1bOQ\x15my-openai\n" + PASSPHRASE_LINE)
    view = asyncio.create_task(console.handle_line("keys\n"))
    await asyncio.sleep(0.05)
    assert "MY_OPENAI" in harness.broker.vault and "OPENAI_API_KEY" not in harness.broker.vault
    assert harness.broker.config.presets["team"].env["OPENAI_API_KEY"].secret == "MY_OPENAI"
    assert "Renamed to MY_OPENAI" in said[-1]
    reader.feed_data(b"\x1b")
    await asyncio.wait_for(view, timeout=5)
    assert said[-1] == console_module.LEAVE_FULL_SCREEN
    assert not any(PASSPHRASE in text for text in said + harness.echoed)


async def test_keys_view_wrong_passphrase_renames_nothing(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "WRONG_PASSPHRASE_PAUSE_SECONDS", 0)
    console, reader, said = new_console(harness)
    reader.feed_data(b"\x1bOQX\nguess\n\x1b")
    await asyncio.wait_for(console.handle_line("keys\n"), timeout=5)
    assert harness.broker.vault.names() == ["DATABASE_URL", "OPENAI_API_KEY", "TEAM_OPENROUTER_KEY"]
    assert any("wrong_passphrase" in line for line in harness.echoed)
    assert any("Wrong passphrase" in text for text in said)


async def test_requests_wait_while_the_keys_view_is_open_and_lines_are_held(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    reader.feed_data(b"keys\n")
    task = asyncio.create_task(console.run())
    await asyncio.sleep(0.05)
    request = run_request(harness)
    console.say("held line")
    assert "held line" not in said
    await asyncio.sleep(console_module.KEYS_REFRESH_SECONDS + 0.2)
    assert any("1 request waiting" in text for text in said)
    reader.feed_data(b"\x1b")
    await asyncio.sleep(0.05)
    assert said.index("held line") > said.index(console_module.LEAVE_FULL_SCREEN)
    assert console.current is request
    reader.feed_data(b"n\nquit\n")
    await asyncio.wait_for(task, timeout=5)
    assert request.decision.result().outcome == "denied"
