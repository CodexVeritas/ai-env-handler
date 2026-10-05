import asyncio
import os
import re
import stat
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest

from envh.core.audit import Audit, AuditEvent
from envh.core.config import load_config
from envh.core.state import Provenance, Request
from envh.core.vault import Vault
from envh.server.console import app as app_module
from envh.server.console.activity import Activity
from envh.server.console.app import ConsoleApp
from envh.server.console.tui import Paste, text_width
from tests.conftest import PASSPHRASE, Harness

PHRASE = "amber basil cedar"
STYLES = re.compile(r"\x1b\[[0-9;]*m")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Console:
    app: ConsoleApp
    clock: Clock
    written: list[str]

    def press(self, *keys: str | Paste) -> None:
        for key in keys:
            self.app.press(key)

    def type(self, text: str) -> None:
        self.press(*text)

    def command(self, name: str) -> None:
        self.type("/" + name)
        self.press("enter")

    def screen(self, columns: int = 110, rows: int = 34) -> str:
        return "\n".join(self.app.frame(columns, rows))


def new_console(harness: Harness) -> Console:
    activity = Activity(harness.clock)

    def listen(event: AuditEvent) -> None:
        harness.echoed.append(event.line())
        activity.add(event)

    harness.broker.audit = Audit(harness.data_dir / "audit.jsonl", listen, harness.clock)
    clock = Clock()
    written: list[str] = []
    return Console(ConsoleApp(harness.broker, PHRASE, activity, write=written.append, color=False, clock=clock), clock, written)


def run_request(harness: Harness, reason: str | None = "why") -> Request:
    return harness.broker.request_run({"OPENAI_API_KEY": "OPENAI_API_KEY"}, [], reason, ("python", "x.py"), harness.provenance())


async def test_typing_without_a_slash_is_not_shown_and_does_nothing(harness: Harness) -> None:
    console = new_console(harness)
    console.type(PASSPHRASE)
    screen = console.screen()
    assert PASSPHRASE not in screen and "Commands start with /" in screen
    console.press("enter")
    assert len(console.app.views) == 1 and not console.app.dialogs
    assert not any("wrong_passphrase" in line for line in harness.echoed)


async def test_the_palette_finds_commands_and_tab_completes_them(harness: Harness) -> None:
    console = new_console(harness)
    console.type("/pre")
    screen = console.screen()
    assert "› /presets" in screen and "Create and edit presets" in screen
    console.press("tab")
    assert "> /presets" in console.screen()
    console.press("enter")
    assert console.screen().startswith("envh console › Presets")
    console.press("escape")
    console.type("/nope")
    console.press("enter")
    assert "No command /nope" in console.screen()


async def test_up_brings_back_earlier_commands(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("escape")
    console.command("sessions")
    console.press("escape")
    console.press("up")
    assert "> /sessions" in console.screen()
    console.press("up", "up")
    assert "> /keys" in console.screen()
    console.press("down")
    assert "> /sessions" in console.screen()
    console.press("down")
    assert "Type / for commands" in console.screen()
    console.press("up", "up", "enter")
    assert console.screen().startswith("envh console › Keys")
    assert console.app.history == ["/keys", "/sessions", "/keys"]


async def test_a_request_takes_over_and_the_passphrase_approves_it(harness: Harness) -> None:
    console = new_console(harness)
    request = run_request(harness)
    screen = console.screen()
    assert f"Run request #{request.id}" in screen and f"[{PHRASE}] Vault passphrase" in screen
    assert "python x.py" in screen and "OPENAI_API_KEY ← OPENAI_API_KEY" in screen and '"why"' in screen
    assert console.written == ["\a"]
    console.type(PASSPHRASE)
    assert PASSPHRASE not in console.screen() and "•••" in console.screen()
    console.press("enter")
    assert request.decision.result().outcome == "approved"
    assert console.app.card is None and f"Approved #{request.id}" in console.screen()
    assert not any(PASSPHRASE in line for line in harness.echoed)


async def test_requests_come_one_at_a_time_and_down_or_esc_then_enter_denies(harness: Harness) -> None:
    console = new_console(harness)
    first, second = run_request(harness), run_request(harness, reason=None)
    assert f"╭─ Run request #{first.id}" in console.screen() and f"╭─ Run request #{second.id}" not in console.screen()
    console.press("down", "enter")
    assert first.decision.result().outcome == "denied"
    assert f"╭─ Run request #{second.id}" in console.screen() and "none given. Ask why before approving." in console.screen()
    console.press("escape")
    assert "› Deny" in console.screen() and second.pending
    console.press("enter")
    assert second.decision.result().outcome == "denied"
    screen = console.screen()
    assert f"Run request #{second.id} · no reason given" in screen and f"Denied #{second.id}" in screen


async def test_a_wrong_passphrase_is_audited_and_pauses_typing(harness: Harness) -> None:
    console = new_console(harness)
    request = run_request(harness)
    console.type("guess-one")
    console.press("enter")
    assert request.pending and "Wrong passphrase" in console.screen()
    assert sum("wrong_passphrase" in line for line in harness.echoed) == 1
    console.type(PASSPHRASE)
    console.press("enter")
    assert request.pending
    console.clock.now += app_module.WRONG_PASSPHRASE_PAUSE_SECONDS
    console.type(PASSPHRASE)
    console.press("enter")
    assert request.decision.result().outcome == "approved"
    assert not any("guess-one" in line for line in [*harness.echoed, console.screen()])


async def test_a_withdrawn_request_swallows_what_is_typed_until_enter(harness: Harness) -> None:
    console = new_console(harness)
    withdrawn = run_request(harness)
    waiting = run_request(harness)
    console.type(PASSPHRASE[:4])
    harness.broker.state.withdraw(withdrawn)
    await asyncio.sleep(0)
    assert "Withdrawn: the program that asked went away" in console.screen()
    console.type(PASSPHRASE[4:])
    console.press("enter")
    assert waiting.pending and console.app.card is not None and console.app.card.request is waiting
    console.type(PASSPHRASE)
    console.press("enter")
    assert waiting.decision.result().outcome == "approved"


async def test_a_request_waits_while_something_is_being_typed(harness: Harness) -> None:
    console = new_console(harness)
    console.type("/ke")
    request = run_request(harness)
    assert console.app.card is None and "1 request waiting" in console.screen()
    console.press("escape")
    assert console.app.card is not None and console.app.card.request is request


async def test_the_bell_rings_for_a_new_request_and_again_while_it_waits(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "reminder_delays", lambda: iter([30, 60, 120]))
    console = new_console(harness)
    run_request(harness)
    console.app.tick()
    console.clock.now += 29
    console.app.tick()
    assert console.written == ["\a"]
    console.clock.now += 2
    console.app.tick()
    assert console.written == ["\a", "\a"]
    harness.broker.config.notify = False
    console.clock.now += 100
    console.app.tick()
    run_request(harness)
    assert console.written == ["\a", "\a"]


async def test_keys_show_the_ends_of_values_where_they_are_used_and_never_a_value(harness: Harness) -> None:
    harness.broker.vault.set("LONG_KEY", "sk-proj-" + "x" * 40 + "Zq9f")
    console = new_console(harness)
    console.command("keys")
    screen = console.screen()
    assert "sk-p…Zq9f" in screen and "p…x" in screen and "dbwork, team" in screen and "per-run" not in screen
    assert not any(value in screen for value in ("sk-openai", "sk-or-team", "postgres://x", "x" * 40))


async def test_f2_renames_a_key_after_the_passphrase(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("down", "f2", "ctrl_u")
    console.type("my-openai")
    console.press("enter")
    screen = console.screen()
    assert "Rename OPENAI_API_KEY to MY_OPENAI" in screen and f"[{PHRASE}] Vault passphrase" in screen
    console.type(PASSPHRASE)
    console.press("enter")
    assert "MY_OPENAI" in harness.broker.vault and "OPENAI_API_KEY" not in harness.broker.vault
    assert harness.broker.config.presets["team"].env["OPENAI_API_KEY"].secret == "MY_OPENAI"
    assert "Renamed OPENAI_API_KEY to MY_OPENAI" in console.screen()


async def test_a_refused_name_ends_the_prompt_so_typing_ahead_lands_nowhere(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("f2", "ctrl_u")
    console.type("team_openrouter_key")
    console.press("enter")
    assert "already taken" in console.screen() and not console.app.dialogs
    console.type("hunter22")
    assert "hunter22" not in console.screen().lower() and harness.broker.vault.names() == ["DATABASE_URL", "OPENAI_API_KEY", "TEAM_OPENROUTER_KEY"]


async def test_add_stores_a_key_whose_value_never_shows(harness: Harness) -> None:
    console = new_console(harness)
    console.command("add")
    console.type("new key")
    console.press("enter", Paste("sk-proj-new-value-1234567890\n"))
    assert "sk-proj-new-value" not in console.screen()
    console.press("enter")
    screen = console.screen()
    assert "Store NEW_KEY" in screen and "sk…90  28 characters" in screen
    console.type(PASSPHRASE)
    console.press("enter")
    assert harness.broker.vault.get("NEW_KEY") == "sk-proj-new-value-1234567890"
    assert "Stored NEW_KEY" in console.screen()
    assert not any("sk-proj-new-value" in line for line in harness.echoed)


async def test_a_description_is_saved_and_shown_to_clients(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("enter", "down", "enter")
    console.type("Production database, read-only")
    console.press("enter")
    console.type(PASSPHRASE)
    console.press("enter")
    assert harness.broker.config.description_for("DATABASE_URL") == "Production database, read-only"
    assert "Production database, read-only" in console.screen()
    assert harness.broker.list_payload()["secrets"][0]["description"] == "Production database, read-only"
    assert "Production database, read-only" in (harness.data_dir / "config.yaml").read_text()


async def test_a_key_a_preset_uses_cannot_be_removed(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("delete")
    assert "used by preset dbwork" in console.screen() and not console.app.dialogs


async def test_presets_change_in_a_draft_saved_with_one_passphrase(harness: Harness) -> None:
    console = new_console(harness)
    console.command("presets")
    console.press("down", "enter", "end", "enter")
    console.type("DATABASE_URL")
    console.press("enter", "enter", "home", "enter", "ctrl_u")
    console.type("90m")
    console.press("enter")
    assert "1 preset changed and not saved yet" in console.screen()
    console.press("ctrl_s")
    screen = console.screen()
    assert "+ DATABASE_URL ← DATABASE_URL" in screen and "~ longest session 3h → 90m" in screen
    console.type(PASSPHRASE)
    console.press("enter")
    preset = harness.broker.config.presets["team"]
    assert preset.env["DATABASE_URL"].secret == "DATABASE_URL" and preset.max_session == timedelta(minutes=90)
    assert "Saved the presets" in console.screen()
    assert load_config(harness.data_dir, set(harness.broker.vault.names())).presets["team"].max_session == timedelta(minutes=90)


async def test_leaving_presets_with_unsaved_changes_asks_first(harness: Harness) -> None:
    console = new_console(harness)
    console.command("presets")
    console.press("end", "enter")
    console.type("research")
    console.press("enter")
    console.type("OPENAI_API_KEY")
    console.press("enter", "enter", "escape")
    assert "research" in console.screen() and "(new)" in console.screen()
    console.press("escape")
    screen = console.screen()
    assert "1 preset changed" in screen and "+ research (new)" in screen
    console.press("down", "enter")
    assert "research" not in harness.broker.config.presets
    assert not console.screen().startswith("envh console › Presets")


async def test_settings_show_every_option_and_save_with_the_passphrase(harness: Harness) -> None:
    console = new_console(harness)
    console.command("settings")
    screen = console.screen()
    assert all(label in screen for label in ("Notifications", "Allowed users", "Approval", "Longest session", "OPENAI_API_KEY", "Add a rule"))
    console.press("enter", "down", "down", "down", "enter", "ctrl_u")
    console.type("4h")
    console.press("enter", "ctrl_s")
    screen = console.screen()
    assert "Notifications: on → off" in screen and "Default longest session: 2h → 4h" in screen
    console.type(PASSPHRASE)
    console.press("enter")
    reloaded = load_config(harness.data_dir, set(harness.broker.vault.names()))
    assert reloaded.notify is False and reloaded.defaults.max_session == timedelta(hours=4)
    assert reloaded.policy_for("OPENAI_API_KEY").max_session == timedelta(hours=1)


async def test_a_wrong_passphrase_saves_nothing(harness: Harness) -> None:
    console = new_console(harness)
    before = (harness.data_dir / "config.yaml").read_text()
    console.command("settings")
    console.press("enter", "ctrl_s")
    console.type("not-the-passphrase")
    console.press("enter")
    assert "Wrong passphrase. Typing is paused for 2 s." in console.screen()
    console.clock.now += app_module.WRONG_PASSPHRASE_PAUSE_SECONDS
    assert "Wrong passphrase. Nothing changed." in console.screen()
    assert (harness.data_dir / "config.yaml").read_text() == before and harness.broker.config.notify is True


async def test_a_key_gets_its_own_rule_from_its_screen(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("enter", "down", "down", "down", "enter")
    assert "Rule for DATABASE_URL" in console.screen()
    console.press("enter", "down", "down", "enter", "ctrl_s")
    assert "+ Rule for DATABASE_URL: per-run" in console.screen()
    console.type(PASSPHRASE)
    console.press("enter")
    assert harness.broker.config.policy_for("DATABASE_URL").approval == "per-run"


async def test_a_live_session_can_be_ended_from_the_console(harness: Harness) -> None:
    mapping, presets = harness.broker.mapping_from_presets(["team"], [])
    request = harness.broker.request_session(mapping, presets, 30, "work", ("x",), harness.provenance())
    harness.broker.approve(request)
    console = new_console(harness)
    console.command("sessions")
    assert request.result["session_id"][:8] in console.screen()
    console.press("enter", "down", "enter")
    assert harness.broker.state.live_sessions() == []
    assert f"Ended session {request.result['session_id'][:8]}" in console.screen()


async def test_changing_the_passphrase_reencrypts_the_vault(harness: Harness) -> None:
    console = new_console(harness)
    console.command("passphrase")
    console.type(PASSPHRASE)
    console.press("enter")
    assert f"[{PHRASE}]" in console.screen()
    console.type("new-vault-passphrase")
    console.press("enter")
    console.type("new-vault-passphrase")
    console.press("enter")
    assert Vault.open(harness.data_dir / "vault.age", "new-vault-passphrase").names() == harness.broker.vault.names()
    request = run_request(harness)
    console.type(PASSPHRASE)
    console.press("enter")
    assert request.pending
    console.clock.now += app_module.WRONG_PASSPHRASE_PAUSE_SECONDS
    console.type("new-vault-passphrase")
    console.press("enter")
    assert request.decision.result().outcome == "approved"


async def test_editing_presets_in_an_editor_is_checked_before_saving(harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    editor = tmp_path / "editor.sh"
    editor.write_text(
        "#!/bin/sh\n"
        f"if [ -e {tmp_path}/once ]; then printf 'presets: {{edited: {{env: {{X: OPENAI_API_KEY}}}}}}\\n' > \"$1\"\n"
        f"else touch {tmp_path}/once; printf 'presets: {{broken: {{env: {{X: NOPE}}}}}}\\n' > \"$1\"; fi\n"
    )
    os.chmod(editor, stat.S_IRWXU)
    monkeypatch.setenv("VISUAL", str(editor))
    console = new_console(harness)
    console.command("edit-presets")
    console.type(PASSPHRASE)
    console.press("enter")
    assert "presets.yaml has a problem" in console.screen() and "not in the vault" in console.screen()
    console.press("enter")
    screen = console.screen()
    assert "Save presets.yaml?" in screen and "+ edited (new)" in screen and "- team (removed)" in screen
    console.press("enter")
    assert list(harness.broker.config.presets) == ["edited"]
    assert not (harness.data_dir / "presets.yaml.edit").exists()
    assert any("config_edited" in line for line in harness.echoed)


async def test_ctrl_c_twice_stops_the_console(harness: Harness) -> None:
    console = new_console(harness)
    console.press("ctrl_c")
    assert not console.app.quit_requested.is_set() and "Press Ctrl-C again" in console.screen()
    console.clock.now += 3
    console.press("ctrl_c")
    assert not console.app.quit_requested.is_set()
    console.press("ctrl_c")
    assert console.app.quit_requested.is_set()


async def test_an_import_card_shows_names_and_fingerprints_never_values(harness: Harness) -> None:
    console = new_console(harness)
    secrets = {"OPENAI_API_KEY": "sk-other-project-value", "SAME_KEY": "sk-openai", "FRESH_KEY": "fresh-value-123456"}
    harness.broker.request_import(secrets, {"fresh": {"env": {"FRESH": "FRESH_KEY"}}}, None, harness.provenance())
    screen = console.screen(120, 44)
    assert "+ OPENAI_API_KEY_2" in screen and "sent as OPENAI_API_KEY" in screen
    assert "same value already stored as OPENAI_API_KEY" in screen and "+ fresh (new)" in screen
    assert not any(value in screen for value in secrets.values())


async def test_a_preset_proposal_card_says_what_would_change(harness: Harness) -> None:
    console = new_console(harness)
    harness.broker.request_preset("presets: {team: {env: {OPENAI_API_KEY: DATABASE_URL}}}", "repoint", harness.provenance())
    screen = console.screen(120, 44)
    assert "~ team" in screen and "OPENAI_API_KEY ← DATABASE_URL (was OPENAI_API_KEY)" in screen
    assert "- OPENROUTER_API_KEY ← TEAM_OPENROUTER_KEY" in screen and "longest session 3h → not set" in screen


async def test_requester_text_cannot_move_the_cursor_or_restyle_the_screen(harness: Harness) -> None:
    console = new_console(harness)
    console.app.color = True
    provenance = Provenance(pid=1, uid=harness.provenance().uid, cmdline="envh\x1b[2J session‮ start")
    harness.broker.request_run({"DATABASE_URL": "DATABASE_URL"}, [], "backfill\x1b[1A\x1b[2K\nfake line", ("envh", "run\r--with"), provenance)
    for line in console.app.frame(110, 34):
        assert "\x1b" not in STYLES.sub("", line) and not any(char in line for char in "\r\n‮")


async def test_every_frame_fits_the_window(harness: Harness) -> None:
    console = new_console(harness)
    run_request(harness, reason="x" * 300)
    for columns, rows in ((50, 14), (80, 24), (132, 40)):
        lines = console.app.frame(columns, rows)
        assert len(lines) <= rows and all(text_width(line) <= columns for line in lines)
    assert "Make this window bigger" in console.screen(40, 10)


@pytest.mark.parametrize("command", ["keys", "presets", "settings", "sessions", "help", "add", "passphrase", "quit"])
async def test_every_screen_draws_in_the_smallest_window_and_a_big_one(harness: Harness, command: str) -> None:
    console = new_console(harness)
    console.command(command)
    console.press("enter")
    run_request(harness, reason="a long reason " * 20)
    for columns, rows in ((app_module.MIN_COLUMNS, app_module.MIN_ROWS), (200, 60)):
        lines = console.app.frame(columns, rows)
        assert len(lines) <= rows and all(text_width(line) <= columns for line in lines)
        assert "could not be drawn" not in "\n".join(lines)


async def test_enter_after_typing_in_a_list_opens_nothing(harness: Harness) -> None:
    console = new_console(harness)
    console.command("keys")
    console.press("end")
    for _ in range(2):
        console.type(PASSPHRASE)
        console.press("enter")
        assert not console.app.dialogs and "Enter did nothing either" in console.screen()
    assert PASSPHRASE.upper().replace("-", "_") not in console.screen()
    console.press("up", "down", "enter")
    assert console.app.dialogs and "Add a key" in console.screen()

