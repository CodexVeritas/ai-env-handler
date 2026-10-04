import asyncio
import io
import os
import re
import stat
from pathlib import Path

import pytest

from envh.core.state import Provenance, Request
from envh.core.vault import Vault
from envh.server import console as console_module
from envh.server.console import COMMAND_PROMPT, ERASE_LINE, Console, ConsoleOutput, parse_line, render_request
from tests.conftest import ME, PASSPHRASE, PASSPHRASE_LINE, Harness

PHRASE = "amber basil cedar"


def test_parse_line_table() -> None:
    assert parse_line("  ").kind == "empty"
    assert parse_line("add KEY").command == "add" and parse_line("add KEY").args == ("KEY",)
    assert parse_line("4821").kind == "command"


async def test_render_request_shows_reason_and_the_passphrase_prompt(harness: Harness) -> None:
    mapping, presets = harness.broker.mapping_from_presets(["dbwork"], [])
    request = harness.broker.request_session(mapping, presets, 30, None, ("envh", "session", "start", "dbwork"), harness.provenance())
    text = "\n".join(render_request(request, harness.clock()))
    assert "no reason given" in text
    assert "OPENAI_API_KEY" in text and "excluded" in text and "DATABASE_URL" in text
    assert "sk-openai" not in text



async def test_render_request_names_every_preset(harness: Harness) -> None:
    mapping, presets = harness.broker.mapping_from_presets(["team", "dbwork"], [])
    combined = harness.broker.request_run(mapping, presets, "mixed", ("python", "x.py"), harness.provenance())
    single = harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, presets[:1], "one", ("python", "x.py"), harness.provenance())
    assert "   presets:  team, dbwork" in render_request(combined, harness.clock())
    assert "   preset:   team" in render_request(single, harness.clock())

def new_console(harness: Harness) -> tuple[Console, asyncio.StreamReader, list[str]]:
    reader = asyncio.StreamReader()
    said: list[str] = []
    return Console(harness.broker, reader, ConsoleOutput(said.append, said.append), tty_fd=None, phrase=PHRASE), reader, said


def run_request(harness: Harness, reason: str | None = "why") -> Request:
    return harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, [], reason, ("python", "x.py"), harness.provenance())


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
    console, reader, said = new_console(harness)
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
    console, reader, said = new_console(harness)
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
    console, reader, said = new_console(harness)
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
    console, reader, said = new_console(harness)
    reader.feed_data(b"edit presets definitely-not-an-editor\n")
    reader.feed_data(b"help\n")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(console.run(), timeout=10)
    assert any("not found" in line for line in said)
    assert any("commands:" in line for line in said)


async def test_command_prompt_stays_below_log_lines_and_gives_way_to_requests(harness: Harness) -> None:
    output = io.StringIO()
    console_output = ConsoleOutput(lambda text: output.write(text + "\n"), output.write)
    reader = asyncio.StreamReader()
    console = Console(harness.broker, reader, console_output, tty_fd=None, phrase=PHRASE)
    task = asyncio.create_task(console.run())
    await asyncio.sleep(0)
    assert output.getvalue().endswith(f"help for commands\n{COMMAND_PROMPT}")
    console_output.say("[14:00:00] run_start run=1")
    assert output.getvalue().endswith(f"{COMMAND_PROMPT}{ERASE_LINE}[14:00:00] run_start run=1\n{COMMAND_PROMPT}")
    request = run_request(harness)
    assert f"{COMMAND_PROMPT}{ERASE_LINE}\a[" in output.getvalue()
    assert output.getvalue().endswith("or n to deny:\n")
    reader.feed_data(b"n\n")
    await asyncio.sleep(0.05)
    assert request.decision.result().outcome == "denied"
    assert output.getvalue().endswith(f"denied #{request.id}\n{COMMAND_PROMPT}")
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(task, timeout=5)
    assert not output.getvalue().endswith(COMMAND_PROMPT)


async def test_render_request_neutralizes_control_characters(harness: Harness) -> None:
    reason = "backfill\n   OPENAI_API_KEY <- OPENAI_API_KEY\x1b[1A\x1b[2K"
    provenance = Provenance(pid=1, uid=harness.provenance().uid, cmdline="envh\x1b[2J session start")
    request = harness.broker.request_run({"DATABASE_URL": "DATABASE_URL"}, [], reason, ("envh", "run\r--with"), provenance)
    lines = render_request(request, harness.clock())
    assert len(lines) == len("\n".join(lines).splitlines())
    assert not any("\x1b" in line or "\r" in line for line in lines)
    assert lines[0].startswith("[") and "DATABASE_URL" in "\n".join(lines)
    assert not any("\x1b" in line or "\n" in line for line in harness.echoed)


async def test_edit_config_without_users_is_rejected(harness: Harness, tmp_path: Path) -> None:
    console, reader, said = new_console(harness)
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
    console, reader, said = new_console(harness)
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
    console, reader, said = new_console(harness)
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
    await asyncio.sleep(console_module.ESCAPE_WAIT_SECONDS + 0.2)
    assert said.index("held line") > said.index(console_module.LEAVE_FULL_SCREEN)
    assert console.current is request
    reader.feed_data(b"n\nquit\n")
    await asyncio.wait_for(task, timeout=5)
    assert request.decision.result().outcome == "denied"


async def test_a_new_request_rings_the_bell_unless_notify_is_off(harness: Harness) -> None:
    console, _, said = new_console(harness)
    run_request(harness)
    assert said[0].startswith("\a")
    harness.broker.request_listeners.remove(console.on_request)
    harness.broker.config.notify = False
    _, _, quiet_said = new_console(harness)
    run_request(harness)
    assert quiet_said and not any("\a" in line for line in quiet_said)


async def test_the_console_rings_again_while_a_request_waits(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(console_module, "reminder_delays", lambda: iter([0.02] * 100))
    said: list[str] = []
    drawn: list[str] = []
    console = Console(harness.broker, asyncio.StreamReader(), ConsoleOutput(said.append, drawn.append), tty_fd=None, phrase=PHRASE)
    run_request(harness)
    await asyncio.sleep(0.15)
    assert drawn.count("\a") >= 3
    await console.handle_line("n\n")
    rings = drawn.count("\a")
    await asyncio.sleep(0.1)
    assert drawn.count("\a") == rings
    harness.broker.config.notify = False
    run_request(harness)
    await asyncio.sleep(0.1)
    assert drawn.count("\a") == rings


async def test_a_rename_that_fails_after_saving_says_so_and_shows_the_new_name(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    console, reader, said = new_console(harness)

    def renames_then_fails(old: str, new: str) -> None:
        harness.broker.vault.rename(old, new)
        raise OSError("No space left on device")

    monkeypatch.setattr(harness.broker, "rename_secret", renames_then_fails)
    reader.feed_data(b"\x1b[B\x1bOQ\x15my_openai\n" + PASSPHRASE_LINE)
    view = asyncio.create_task(console.handle_line("keys\n"))
    await asyncio.sleep(0.2)
    assert "Renamed to MY_OPENAI, but: No space left on device" in said[-1]
    assert "MY_OPENAI" in said[-1].split("Name")[1]
    reader.feed_data(b"\x1b")
    await asyncio.wait_for(view, timeout=5)


async def test_keys_typed_after_esc_in_the_same_read_reach_the_waiting_request(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    reader.feed_data(b"keys\n")
    task = asyncio.create_task(console.run())
    await asyncio.sleep(0.05)
    request = run_request(harness)
    reader.feed_data(b"\x1bn\n")
    await asyncio.sleep(0.2)
    assert request.decision.result().outcome == "denied"
    reader.feed_data(b"quit\n")
    await asyncio.wait_for(task, timeout=5)


async def test_an_arrow_key_split_across_reads_moves_instead_of_leaving(harness: Harness) -> None:
    console, reader, said = new_console(harness)
    view = asyncio.create_task(console.handle_line("keys\n"))
    await asyncio.sleep(0.05)
    reader.feed_data(b"\x1b")
    await asyncio.sleep(0.01)
    reader.feed_data(b"[B")
    await asyncio.sleep(0.2)
    assert not view.done()
    assert "› OPENAI_API_KEY" in re.sub(r"\x1b\[[0-9;]*m", "", said[-1])
    reader.feed_data(b"\x1b")
    await asyncio.wait_for(view, timeout=5)


def test_held_output_keeps_the_newest_lines_and_tolerates_nesting() -> None:
    printed: list[str] = []
    output = ConsoleOutput(printed.append, printed.append)
    with output.holding():
        with output.holding():
            output.say("inner")
        for number in range(console_module.HELD_LINES_KEPT + 5):
            output.say(f"line {number}")
        assert printed == []
    assert printed[0] == "(6 earlier lines were not kept; every audit event is in audit.jsonl)"
    assert printed[1] == "line 5" and printed[-1] == f"line {console_module.HELD_LINES_KEPT + 4}"
