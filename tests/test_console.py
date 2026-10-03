import asyncio
import os
import stat
from pathlib import Path

from envh.server.console import Console, parse_line, render_request
from tests.conftest import Harness


def test_parse_line_table() -> None:
    assert parse_line("4821").kind == "approve"
    assert parse_line("n4821") == parse_line("n4821") and parse_line("n4821").kind == "deny"
    assert parse_line("  ").kind == "empty"
    assert parse_line("add KEY").command == "add" and parse_line("add KEY").args == ("KEY",)
    assert parse_line("12345").kind == "command"
    assert parse_line("y").kind == "command"


async def test_render_request_shows_code_and_reason(harness: Harness) -> None:
    mapping, preset = harness.broker.mapping_from_preset("dbwork")
    request = harness.broker.request_session(mapping, preset, 30, None, ("envh", "session", "start", "dbwork"), harness.provenance())
    text = "\n".join(render_request(request, harness.clock()))
    assert f"code {request.code}" in text
    assert "no reason given" in text
    assert "OPENAI_API_KEY" in text and "excluded" in text and "DATABASE_URL" in text
    assert "sk-openai" not in text


async def test_console_approve_deny_and_unknown_code(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    request = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, None, "why", ("python", "x.py"), harness.provenance())
    assert any(f"code {request.code}" in line for line in said)
    await console.handle_line("0000" if request.code != "0000" else "0001")
    assert any("no pending request" in line for line in said)
    await console.handle_line(request.code)
    assert request.decision.result().outcome == "approved"
    other = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, None, None, (), harness.provenance())
    await console.handle_line(f"n{other.code}")
    assert other.decision.result().outcome == "denied"


async def test_console_admin_add_rm_reload_quit(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    reader.feed_data(b"add NEW_SECRET\n")
    reader.feed_data(b"super-secret-value\n")
    reader.feed_data(b"y\n")
    reader.feed_data(b"rm NEW_SECRET\n")
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
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    reader.feed_data(b"add LATER_KEY\n")
    task = asyncio.create_task(console.run())
    await asyncio.sleep(0.05)
    request = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, None, None, (), harness.provenance())
    assert not any(f"code {request.code}" in line for line in said)
    reader.feed_data(b"value\n")
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(task, timeout=5)
    assert any(f"code {request.code}" in line for line in said)


async def test_edit_presets_validates_before_saving(harness: Harness, tmp_path: Path) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    bad_editor = tmp_path / "bad_editor.sh"
    bad_editor.write_text("#!/bin/sh\nprintf 'presets: {broken: {env: {X: NOPE}}}\\n' > \"$1\"\n")
    good_editor = tmp_path / "good_editor.sh"
    good_editor.write_text("#!/bin/sh\nprintf 'presets: {edited: {max_session: 2h, env: {X: OPENAI_API_KEY}}}\\n' > \"$1\"\n")
    for script in (bad_editor, good_editor):
        os.chmod(script, stat.S_IRWXU)
    presets_path = harness.data_dir / "presets.yaml"
    before = presets_path.read_text()
    reader.feed_data(f"edit presets {bad_editor}\n".encode())
    reader.feed_data(b"n\n")
    reader.feed_data(f"edit presets {good_editor}\n".encode())
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
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    editor = tmp_path / "editor.sh"
    editor.write_text("#!/bin/sh\nprintf 'defaults: {approval: per-run}\\n' > \"$1\"\n")
    os.chmod(editor, stat.S_IRWXU)
    config_path = harness.data_dir / "config.yaml"
    before = config_path.read_text()
    reader.feed_data(f"edit config {editor}\n".encode())
    reader.feed_data(b"n\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert config_path.read_text() == before
    assert harness.broker.config.defaults.approval == "session"
    assert any("discarded" in line for line in said)


async def test_unknown_editor_and_unexpected_errors_keep_console_alive(harness: Harness) -> None:
    reader = asyncio.StreamReader()
    said: list[str] = []
    console = Console(harness.broker, reader, said.append, tty_fd=None)
    reader.feed_data(b"edit presets definitely-not-an-editor\n")
    reader.feed_data(b"help\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert any("not found" in line for line in said)
    assert any(line.startswith("commands:") for line in said)
