import importlib.util
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from envh import platform

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_wizard", ROOT / "scripts" / "setup_wizard.py")
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

HOOK_COMMAND = "python3 /home/alice/.claude/hooks/envh_ask.py"
CURSOR_HOOK_COMMAND = "python3 /home/alice/.cursor/hooks/envh_ask.py"


def answer_inputs(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    remaining = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": remaining.pop(0))


def test_hook_entry_matches_the_documented_snippet() -> None:
    snippet = json.loads((ROOT / "claude" / "settings.snippet.json").read_text())
    documented = snippet["optionA_hooks"]["hooks"]["PreToolUse"][0]
    documented["hooks"][0]["command"] = HOOK_COMMAND
    assert wizard.with_envh_hook({}, HOOK_COMMAND)["hooks"]["PreToolUse"] == [documented]


def test_hook_is_appended_after_existing_hooks_and_other_settings_are_kept() -> None:
    existing_hook = {"matcher": "Write", "hooks": [{"type": "command", "command": "bash ~/.claude/hooks/other.sh"}]}
    settings = {"model": "opus", "hooks": {"PreToolUse": [existing_hook], "Stop": []}}
    merged = wizard.with_envh_hook(settings, HOOK_COMMAND)
    assert merged["model"] == "opus"
    assert merged["hooks"]["Stop"] == []
    assert merged["hooks"]["PreToolUse"][0] == existing_hook
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == HOOK_COMMAND
    assert len(settings["hooks"]["PreToolUse"]) == 1


@pytest.mark.parametrize("settings", [[], {"hooks": []}, {"hooks": {"PreToolUse": {}}}])
def test_unexpected_settings_shapes_are_refused(settings: object) -> None:
    with pytest.raises(ValueError):
        wizard.with_envh_hook(settings, HOOK_COMMAND)


def test_an_envh_hook_installed_from_any_path_is_detected() -> None:
    settings = wizard.with_envh_hook({}, "python3 /srv/checkout/claude/hooks/envh_ask.py")
    assert wizard.envh_hook_commands(settings) == ["python3 /srv/checkout/claude/hooks/envh_ask.py"]
    assert wizard.envh_hook_commands({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "x.sh"}]}]}}) == []


def test_settings_hook_text_keeps_other_settings_and_is_none_once_present(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model": "opus", "note": "café"}\n')
    new_text = wizard.config_with_hook(settings_path, HOOK_COMMAND, wizard.with_envh_hook, wizard.envh_hook_commands)
    saved = json.loads(new_text)
    assert saved["note"] == "café"
    assert wizard.envh_hook_commands(saved) == [HOOK_COMMAND]
    settings_path.write_text(new_text)
    assert wizard.config_with_hook(settings_path, HOOK_COMMAND, wizard.with_envh_hook, wizard.envh_hook_commands) is None


def test_invalid_settings_json_stops_with_a_message(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not json")
    with pytest.raises(SystemExit, match="Fix the file"):
        wizard.config_with_hook(settings_path, HOOK_COMMAND, wizard.with_envh_hook, wizard.envh_hook_commands)


def test_cursor_hook_entry_matches_the_documented_snippet() -> None:
    snippet = json.loads((ROOT / "cursor" / "hooks.snippet.json").read_text())
    del snippet["_comment"]
    snippet["hooks"]["beforeShellExecution"][0]["command"] = CURSOR_HOOK_COMMAND
    assert wizard.with_cursor_hook({}, CURSOR_HOOK_COMMAND) == snippet


def test_cursor_rerun_migrates_a_stale_matcher_entry(tmp_path: Path) -> None:
    other = {"command": "bash ~/.cursor/hooks/other.sh"}
    stale = {"version": 1, "hooks": {"beforeShellExecution": [other, {"command": CURSOR_HOOK_COMMAND, "timeout": 10, "matcher": "envh"}]}}
    config_path = tmp_path / "hooks.json"
    config_path.write_text(json.dumps(stale) + "\n")
    new_text = wizard.cursor_config_text(config_path, CURSOR_HOOK_COMMAND)
    assert new_text is not None, "a stale command-text matcher entry must be migrated, not treated as already-installed"
    migrated = json.loads(new_text)
    envh_entries = [e for e in migrated["hooks"]["beforeShellExecution"] if wizard.HOOK_FILE_NAME in str(e.get("command", ""))]
    assert len(envh_entries) == 1 and "matcher" not in envh_entries[0]
    assert other in migrated["hooks"]["beforeShellExecution"]  # unrelated entries are preserved
    config_path.write_text(new_text)  # idempotent: a rerun on the migrated config changes nothing
    assert wizard.cursor_config_text(config_path, CURSOR_HOOK_COMMAND) is None


def test_cursor_hook_is_appended_and_the_rest_of_hooks_json_is_kept() -> None:
    existing_hook = {"command": "./hooks/audit.sh"}
    config = {"version": 1, "hooks": {"beforeShellExecution": [existing_hook], "stop": [{"command": "./hooks/done.sh"}]}}
    merged = wizard.with_cursor_hook(config, CURSOR_HOOK_COMMAND)
    assert merged["hooks"]["stop"] == config["hooks"]["stop"]
    assert merged["hooks"]["beforeShellExecution"][0] == existing_hook
    assert wizard.cursor_hook_commands(merged) == [CURSOR_HOOK_COMMAND]
    assert wizard.cursor_hook_commands(config) == []
    assert len(config["hooks"]["beforeShellExecution"]) == 1


@pytest.mark.parametrize("config", [[], {"hooks": []}, {"hooks": {"beforeShellExecution": {}}}])
def test_unexpected_cursor_hooks_shapes_are_refused(config: object) -> None:
    with pytest.raises(ValueError, match="hooks.json"):
        wizard.with_cursor_hook(config, CURSOR_HOOK_COMMAND)


@pytest.mark.parametrize(
    ("current", "expected"),
    [("", "snippet\n"), ("# Mine\n", "# Mine\n\nsnippet\n"), ("# Mine", "# Mine\n\nsnippet\n")],
)
def test_claude_md_snippet_is_appended_after_one_blank_line(current: str, expected: str) -> None:
    assert wizard.claude_md_with_snippet(current, "snippet\n") == expected


def test_claude_code_changes_apply_once_back_up_settings_and_keep_its_mode(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model": "opus"}\n')
    settings_path.chmod(0o600)
    (tmp_path / "CLAUDE.md").write_text("# Mine\n")
    changes = wizard.claude_code_changes(tmp_path)
    assert len(changes) == 4
    for change in changes:
        change.apply()
    assert (tmp_path / "skills" / "envh" / "SKILL.md").read_text() == (ROOT / "claude" / "skills" / "envh" / "SKILL.md").read_text()
    assert wizard.envh_hook_commands(json.loads(settings_path.read_text())) == [f"python3 {tmp_path / 'hooks' / 'envh_ask.py'}"]
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert [backup.read_text() for backup in tmp_path.glob("settings.json.before-envh-*")] == ['{"model": "opus"}\n']
    assert wizard.CLAUDE_MD_MARKER in (tmp_path / "CLAUDE.md").read_text()
    assert wizard.claude_code_changes(tmp_path) == []


def test_cursor_changes_apply_once_and_back_up_hooks_json(tmp_path: Path) -> None:
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text('{"version": 1, "hooks": {}}\n')
    changes = wizard.cursor_changes(tmp_path)
    assert len(changes) == 3
    for change in changes:
        change.apply()
    assert (tmp_path / "skills" / "envh" / "SKILL.md").read_text() == (ROOT / "claude" / "skills" / "envh" / "SKILL.md").read_text()
    assert (tmp_path / "hooks" / "envh_ask.py").read_text() == (ROOT / "claude" / "hooks" / "envh_ask.py").read_text()
    assert wizard.cursor_hook_commands(json.loads(hooks_path.read_text())) == [f"python3 {tmp_path / 'hooks' / 'envh_ask.py'}"]
    assert [backup.read_text() for backup in tmp_path.glob("hooks.json.before-envh-*")] == ['{"version": 1, "hooks": {}}\n']
    assert wizard.cursor_changes(tmp_path) == []


def test_a_home_folder_with_a_space_gets_a_quoted_hook_command(tmp_path: Path) -> None:
    cursor_home = tmp_path / "ann lee" / ".cursor"
    cursor_home.mkdir(parents=True)
    for change in wizard.cursor_changes(cursor_home):
        change.apply()
    [command] = wizard.cursor_hook_commands(json.loads((cursor_home / "hooks.json").read_text()))
    assert shlex.split(command) == ["python3", str(cursor_home / "hooks" / "envh_ask.py")]


def test_a_changed_skill_is_offered_as_an_update(tmp_path: Path) -> None:
    for change in wizard.claude_code_changes(tmp_path):
        change.apply()
    (tmp_path / "skills" / "envh" / "SKILL.md").write_text("old\n")
    [change] = wizard.claude_code_changes(tmp_path)
    assert change.description.startswith("Update the envh skill")
    assert change.outdated == "skill"


def test_an_update_replaces_the_skill_and_hook_and_leaves_an_edited_claude_md_alone(tmp_path: Path) -> None:
    for change in wizard.claude_code_changes(tmp_path):
        change.apply()
    edited = "# Mine\n\n## Secrets\n- Ask me before using envh.\n"
    (tmp_path / "CLAUDE.md").write_text(edited)
    assert wizard.claude_code_changes(tmp_path) == []
    (tmp_path / "skills" / "envh" / "SKILL.md").write_text("old\n")
    (tmp_path / "hooks" / "envh_ask.py").write_text("old\n")
    changes = wizard.claude_code_changes(tmp_path)
    assert [change.outdated for change in changes] == ["skill", "hook"]
    for change in changes:
        change.apply()
    assert (tmp_path / "hooks" / "envh_ask.py").read_text() == (ROOT / "claude" / "hooks" / "envh_ask.py").read_text()
    assert (tmp_path / "CLAUDE.md").read_text() == edited


def test_an_edited_claude_md_is_left_alone_with_a_warning_naming_the_lines_it_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard, "CLAUDE_HOME", tmp_path)
    for change in wizard.claude_code_changes(tmp_path):
        change.apply()
    snippet_lines = (ROOT / "claude" / "CLAUDE.snippet.md").read_text().splitlines()
    heading = next(text for text in snippet_lines if text.startswith("#"))
    first_bullet, *other_bullets = [text for text in snippet_lines if text.startswith("- ")]
    edited = "## My keys\n- Use envh sessions.\n" + "".join(f"{bullet}\n" for bullet in other_bullets)
    (tmp_path / "CLAUDE.md").write_text(edited)
    assert wizard.step_claude_code() == wizard.Outcome(True, "up to date")
    output = capsys.readouterr().out
    assert "CLAUDE.md lacks these envh lines" in output
    assert first_bullet in output
    assert heading not in output and not any(bullet in output for bullet in other_bullets)
    assert (tmp_path / "CLAUDE.md").read_text() == edited


def test_a_first_connection_adds_the_claude_md_lines_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(wizard, "CLAUDE_HOME", tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Mine\n")
    answer_inputs(monkeypatch, "")
    assert wizard.step_claude_code() == wizard.Outcome(True, "connected")
    assert "lacks these envh lines" not in capsys.readouterr().out
    assert wizard.missing_claude_md_lines(tmp_path / "CLAUDE.md") == []


@pytest.mark.parametrize(("changes_for", "tool"), [(wizard.claude_code_changes, "Claude Code"), (wizard.cursor_changes, "Cursor")])
def test_an_out_of_date_skill_and_hook_are_named_and_declining_keeps_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], changes_for: Callable, tool: str
) -> None:
    for change in changes_for(tmp_path):
        change.apply()
    (tmp_path / "skills" / "envh" / "SKILL.md").write_text("old\n")
    (tmp_path / "hooks" / "envh_ask.py").write_text("old\n")
    answer_inputs(monkeypatch, "n")
    assert wizard.step_connect(tool, tmp_path, changes_for) == wizard.Outcome(False, "out of date")
    assert f"Your envh skill and hook for {tool} are out of date." in capsys.readouterr().out
    assert (tmp_path / "skills" / "envh" / "SKILL.md").read_text() == "old\n"


def test_updating_reports_updated_and_a_rerun_is_up_to_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    for change in wizard.cursor_changes(tmp_path):
        change.apply()
    (tmp_path / "hooks" / "envh_ask.py").write_text("old\n")
    answer_inputs(monkeypatch, "")
    assert wizard.step_connect("Cursor", tmp_path, wizard.cursor_changes) == wizard.Outcome(True, "updated")
    assert "Your envh hook for Cursor is out of date." in capsys.readouterr().out
    assert wizard.step_connect("Cursor", tmp_path, wizard.cursor_changes) == wizard.Outcome(True, "up to date")


def test_a_first_connection_asks_to_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "n")
    assert wizard.step_connect("Cursor", tmp_path, wizard.cursor_changes) == wizard.Outcome(False, "skipped")
    assert len(prompts) == 1 and "Connect Cursor?" in prompts[0]
    assert "out of date" not in capsys.readouterr().out


def test_a_tool_that_is_not_set_up_is_skipped(tmp_path: Path) -> None:
    assert wizard.step_connect("Cursor", tmp_path / "missing", wizard.cursor_changes) == wizard.Outcome(False, "skipped")
    assert not (tmp_path / "missing").exists()


def editor_user_folders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *present: str) -> dict[str, Path]:
    folders = {editor: tmp_path / editor / "User" for editor in wizard.EDITOR_USER_FOLDERS}
    for editor in present:
        folders[editor].mkdir(parents=True)
    monkeypatch.setattr(wizard, "EDITOR_USER_FOLDERS", folders)
    return folders


def test_env_history_setting_is_merged_and_the_file_backed_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folders = editor_user_folders(monkeypatch, tmp_path, "VS Code")
    settings_path = folders["VS Code"] / "settings.json"
    original = '{"editor.fontSize": 14, "workbench.localHistory.exclude": {"**/secrets/**": true}}\n'
    settings_path.write_text(original)
    answer_inputs(monkeypatch, "")
    assert wizard.step_editor_history() == wizard.Outcome(True, "turned off")
    saved = json.loads(settings_path.read_text())
    assert saved["editor.fontSize"] == 14
    assert saved["workbench.localHistory.exclude"] == {"**/secrets/**": True, "**/.env*": True}
    assert [backup.read_text() for backup in settings_path.parent.glob("settings.json.before-envh-*")] == [original]
    assert not folders["Cursor"].exists()


def test_a_second_env_history_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folders = editor_user_folders(monkeypatch, tmp_path, "VS Code", "Cursor")
    (folders["VS Code"] / "settings.json").write_text('{"editor.fontSize": 14}\n')
    answer_inputs(monkeypatch, "")
    assert wizard.step_editor_history() == wizard.Outcome(True, "turned off")
    settings_paths = [folders["VS Code"] / "settings.json", folders["Cursor"] / "settings.json"]
    written = [settings_path.read_text() for settings_path in settings_paths]
    answer_inputs(monkeypatch)
    assert wizard.step_editor_history() == wizard.Outcome(True, "already off")
    assert [settings_path.read_text() for settings_path in settings_paths] == written
    assert len(list(tmp_path.rglob("*.before-envh-*"))) == 1


def test_a_settings_file_with_comments_is_left_alone_with_the_line_to_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = editor_user_folders(monkeypatch, tmp_path, "VS Code", "Cursor")
    cursor_settings = folders["Cursor"] / "settings.json"
    original = b'{\n    // bigger text\n    "editor.fontSize": 14,\n}\n'
    cursor_settings.write_bytes(original)
    answer_inputs(monkeypatch, "")
    assert wizard.step_editor_history() == wizard.Outcome(True, "turned off; add it by hand for Cursor")
    assert cursor_settings.read_bytes() == original
    assert not list(folders["Cursor"].glob("*.before-envh-*"))
    assert json.loads((folders["VS Code"] / "settings.json").read_text()) == {"workbench.localHistory.exclude": {"**/.env*": True}}
    output = capsys.readouterr().out
    assert f"Left {cursor_settings} as is: it isn't plain JSON." in output
    assert '"workbench.localHistory.exclude": {"**/.env*": true},' in output


def test_editors_that_are_not_set_up_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    editor_user_folders(monkeypatch, tmp_path)
    answer_inputs(monkeypatch)
    assert wizard.step_editor_history() == wizard.Outcome(False, "skipped")
    assert "Skipped: no VS Code, VSCodium or Cursor settings found for this user." in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("editor", ["Flatpak VS Code", "Code - OSS", "VSCodium"])
def test_env_history_is_turned_off_in_other_vs_code_builds(
    editor: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    folders = editor_user_folders(monkeypatch, tmp_path, editor)
    answer_inputs(monkeypatch, "")
    assert wizard.step_editor_history() == wizard.Outcome(True, "turned off")
    assert json.loads((folders[editor] / "settings.json").read_text()) == {"workbench.localHistory.exclude": {"**/.env*": True}}
    assert f"Local history in {editor} now leaves out .env files" in capsys.readouterr().out


def test_three_editors_are_named_in_one_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    editor_user_folders(monkeypatch, tmp_path, "VS Code", "VSCodium", "Cursor")
    answer_inputs(monkeypatch, "")
    wizard.step_editor_history()
    assert "Local history in VS Code, VSCodium and Cursor now leaves out .env files" in capsys.readouterr().out


def test_the_history_delete_command_quotes_a_folder_with_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    user_folder = tmp_path / ".config" / "Code - OSS" / "User"
    (user_folder / "History").mkdir(parents=True)
    more = wizard.env_history_more([], [user_folder])
    command = next(text for text in more.splitlines() if text.startswith("rm -r ")).split(" deletes them")[0]
    assert command == "rm -r ~/'.config/Code - OSS/User/History'"
    assert shlex.split(command) == ["rm", "-r", "~/.config/Code - OSS/User/History"]


@pytest.mark.parametrize("settings", [[], {"workbench.localHistory.exclude": ["**/.env*"]}])
def test_unexpected_editor_settings_shapes_are_refused(settings: object) -> None:
    with pytest.raises(ValueError):
        wizard.settings_excluding_env_history(settings)


def test_quit_answer_stops_the_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    answer_inputs(monkeypatch, "maybe", "q")
    with pytest.raises(wizard.Quit):
        wizard.ask("Proceed?", "more")


def test_question_mark_shows_more_then_enter_takes_the_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    answer_inputs(monkeypatch, "?", "")
    assert wizard.ask("Proceed?", "the details", default=False) is False
    assert "the details" in capsys.readouterr().out


def test_verbose_mode_shows_more_without_asking(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(wizard, "verbose", True)
    answer_inputs(monkeypatch, "")
    assert wizard.ask("Proceed?", "the details") is True
    assert "the details" in capsys.readouterr().out


def test_only_the_build_files_are_staged_for_root(tmp_path: Path) -> None:
    wizard.stage_build_files(tmp_path / "staging")
    assert sorted(path.name for path in (tmp_path / "staging").iterdir()) == ["LICENSE", "README.md", "pyproject.toml", "src", "uv.lock"]
    assert not list((tmp_path / "staging").rglob("__pycache__"))


def test_root_half_refuses_to_run_without_sudo() -> None:
    arguments = ["--install-as-root", "--source", "test", "--uv", "/usr/local/bin/uv"]
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "setup_wizard.py"), *arguments], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert "root half" in result.stderr


def test_root_runs_the_wizard_with_the_system_python_and_sudo_by_absolute_path() -> None:
    command = wizard.root_install_command("abc123 from /repo", Path("/usr/local/bin/uv"), ["--ignore-preflight"])
    assert command[:2] == ["/usr/bin/sudo", "/usr/bin/python3"]
    assert command[3:] == ["--install-as-root", "--source", "abc123 from /repo", "--uv", "/usr/local/bin/uv", "--ignore-preflight"]


@pytest.mark.parametrize(("found", "fetch"), [({"curl", "wget"}, "curl -LsSf "), ({"wget"}, "wget -nv -O- ")])
def test_uv_for_root_is_fetched_with_curl_or_else_wget(monkeypatch: pytest.MonkeyPatch, found: set[str], fetch: str) -> None:
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}" if name in found else None)
    assert wizard.uv_root_install_command() == f"{fetch}{wizard.UV_INSTALLER} | /usr/bin/sudo env UV_UNMANAGED_INSTALL=/usr/local/bin sh"
    monkeypatch.setattr(wizard.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="needs curl or wget"):
        wizard.uv_root_install_command()


def test_root_builds_with_the_uv_the_user_half_chose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_uv = tmp_path / "uv"
    monkeypatch.setattr(wizard, "root_owned_uv", lambda: root_uv)
    assert wizard.ensure_uv() == root_uv
    monkeypatch.setattr(wizard, "root_owned_uv", lambda: None)
    monkeypatch.setattr(wizard.Path, "home", lambda: tmp_path)
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "uv").write_text("")
    answer_inputs(monkeypatch, "n", "y")
    assert wizard.ensure_uv() == tmp_path / ".local" / "bin" / "uv"
    answer_inputs(monkeypatch, "n", "n")
    assert wizard.ensure_uv() is None


def test_the_system_checks_load_from_the_staged_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    wizard.stage_build_files(tmp_path / "staging")
    staged_platform = tmp_path / "staging" / "src" / "envh" / "platform.py"
    staged_platform.write_text(staged_platform.read_text() + "\n\ndef preflight_problems(invoking_user):\n    return ['staged copy checked ' + invoking_user]\n")
    assert wizard.system_problems(tmp_path / "staging" / "src", "nobody") == ["staged copy checked nobody"]


def fake_envh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scan_exit: int) -> list[list[str]]:
    envh = tmp_path / "envh"
    envh.write_text("")
    monkeypatch.setattr(wizard, "ENVH_BIN", envh)
    monkeypatch.setattr(wizard, "SCANS_FOLDER", tmp_path / "scans")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append([Path(argv[0]).name, *argv[1:]])
        return subprocess.CompletedProcess(argv, scan_exit if argv[1] == "scan" else 0)

    monkeypatch.setattr(wizard.subprocess, "run", run)
    return calls


def test_a_scan_that_finds_keys_opens_the_explorer_in_the_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_envh(tmp_path, monkeypatch, scan_exit=1)
    answer_inputs(monkeypatch, "", "", "")
    assert wizard.step_scan() == wizard.Outcome(True, "found keys; reviewed in the explorer")
    scans = str(tmp_path / "scans")
    assert calls == [["envh", "scan", "--out", scans, "--no-explore"], ["envh", "explore", scans]]
    shown = capsys.readouterr().out
    assert "╭─ The explorer" in shown and "q      close it and finish setup" in shown and "Reopen it any time with: envh explore" in shown


def test_the_scan_is_the_last_step_so_setup_ends_in_the_explorer() -> None:
    assert wizard.STEPS[-1] == ("Find stray keys", wizard.step_scan)


def test_the_explorer_can_be_skipped_and_a_clean_scan_shows_no_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_envh(tmp_path, monkeypatch, scan_exit=1)
    answer_inputs(monkeypatch, "", str(tmp_path / "elsewhere"), "s")
    assert wizard.step_scan() == wizard.Outcome(True, "found keys; explorer skipped")
    assert calls == [["envh", "scan", "--out", str(tmp_path / "elsewhere"), "--no-explore"]]
    assert "Open it any time with: envh explore" in capsys.readouterr().out
    calls = fake_envh(tmp_path, monkeypatch, scan_exit=0)
    answer_inputs(monkeypatch, "", "")
    assert wizard.step_scan() == wizard.Outcome(True, "nothing found")
    shown = capsys.readouterr().out
    assert "The explorer" not in shown and len(calls) == 1
    assert "No stray keys found in the files it could read" in shown


def test_the_scan_says_what_it_misses_before_it_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_envh(tmp_path, monkeypatch, scan_exit=0)
    answer_inputs(monkeypatch, "n")
    assert wizard.step_scan() == wizard.Outcome(False, "skipped")
    assert "It will miss some: it can't read databases (such as SQLite)" in capsys.readouterr().out


def fake_sudo(monkeypatch: pytest.MonkeyPatch, groups: list[str], failing: str | None = None, remaining: list[str] | None = None) -> list[list[str]]:
    lookups = [groups, remaining or []]
    monkeypatch.setattr(wizard, "root_granting_groups", lambda user: lookups.pop(0) if len(lookups) > 1 else lookups[0])
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if failing in argv else 0, "", "gpasswd: failed")

    monkeypatch.setattr(wizard.subprocess, "run", run)
    return calls


def test_the_root_groups_step_is_first_so_the_install_checks_pass() -> None:
    assert wizard.STEPS[0] == ("Docker group", wizard.step_root_groups)


def test_someone_outside_root_granting_groups_is_not_asked(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_sudo(monkeypatch, [])
    assert wizard.step_root_groups() == wizard.Outcome(True, "not in one")
    assert calls == [] and "not in docker" in capsys.readouterr().out


def test_staying_in_the_docker_group_is_the_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_sudo(monkeypatch, ["docker"])
    answer_inputs(monkeypatch, "")
    assert wizard.step_root_groups() == wizard.Outcome(False, "kept")
    shown = capsys.readouterr().out
    assert calls == [] and "Being in the docker group is the same as having root without a password." in shown


def test_leaving_runs_gpasswd_for_each_group_then_forgets_sudo(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_sudo(monkeypatch, ["docker", "lxd"])
    answer_inputs(monkeypatch, "y")
    user = wizard.pwd.getpwuid(wizard.os.getuid()).pw_name
    assert wizard.step_root_groups() == wizard.Outcome(True, "left; log out and back in")
    assert calls == [[wizard.SUDO, "/usr/bin/gpasswd", "-d", user, "docker"], [wizard.SUDO, "/usr/bin/gpasswd", "-d", user, "lxd"], [wizard.SUDO, "-k"]]
    assert "Left the docker and lxd groups. Log out and back in to finish." in capsys.readouterr().out


def test_a_group_that_remains_after_gpasswd_is_reported_not_left(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake_sudo(monkeypatch, ["docker"], remaining=["docker"])
    answer_inputs(monkeypatch, "y")
    assert wizard.step_root_groups() == wizard.Outcome(False, "still in docker")
    shown = capsys.readouterr().out
    assert "likely as your primary group" in shown and "sudo usermod -g" in shown and "Left the docker group" not in shown


def test_a_failed_gpasswd_stops_the_step_and_still_forgets_sudo(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = fake_sudo(monkeypatch, ["docker"], failing="docker")
    answer_inputs(monkeypatch, "y")
    assert wizard.step_root_groups() == wizard.Outcome(False, "failed to leave docker")
    assert calls[-1] == [wizard.SUDO, "-k"] and "gpasswd: failed" in capsys.readouterr().out


def test_root_granting_groups_come_from_envh_platform_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "modules", dict(sys.modules))
    user = wizard.pwd.getpwuid(wizard.os.getuid()).pw_name
    assert wizard.root_granting_groups(user) == platform.root_granting_groups_of(user)


class FakeKernel:
    def __init__(self, tiocsti: bool | None, ptrace_scope: int | None) -> None:
        self.tiocsti = tiocsti
        self.ptrace_scope = ptrace_scope

    def legacy_tiocsti_enabled(self) -> bool | None:
        return self.tiocsti

    def yama_ptrace_scope(self) -> int | None:
        return self.ptrace_scope


@pytest.mark.parametrize(
    ("tiocsti", "ptrace_scope", "unsafe"),
    [
        (True, 0, ["dev.tty.legacy_tiocsti = 0", "kernel.yama.ptrace_scope = 1"]),
        (False, 0, ["kernel.yama.ptrace_scope = 1"]),
        (False, 1, []),
        (False, 3, []),
        (False, None, []),
    ],
)
def test_only_the_kernel_settings_that_are_unsafe_are_changed(tiocsti: bool, ptrace_scope: int | None, unsafe: list[str]) -> None:
    assert wizard.unsafe_kernel_settings(FakeKernel(tiocsti, ptrace_scope)) == unsafe


def test_kernel_settings_change_only_when_asked_and_are_appended(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    kernel = FakeKernel(tiocsti=False, ptrace_scope=0)
    monkeypatch.setattr(wizard, "load_platform", lambda source: kernel)
    ran: list[tuple[list[str], str | None]] = []

    def fake_run(command: list[str], input: str | None = None, **options: object) -> subprocess.CompletedProcess[str]:
        ran.append((command, input))
        if command[1:2] == [wizard.SYSCTL]:
            kernel.ptrace_scope = 1
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    answer_inputs(monkeypatch, "")
    assert wizard.step_kernel_settings() == wizard.Outcome(False, "kept")
    assert ran == []
    answer_inputs(monkeypatch, "?", "y")
    assert wizard.step_kernel_settings() == wizard.Outcome(True, "changed")
    assert ran == [
        (["/usr/bin/sudo", "/usr/bin/tee", "-a", "/etc/sysctl.d/60-envh.conf"], "kernel.yama.ptrace_scope = 1\n"),
        (["/usr/bin/sudo", "/usr/sbin/sysctl", "-p", "/etc/sysctl.d/60-envh.conf"], None),
        (["/usr/bin/sudo", "-k"], None),
    ]
    assert "gdb -p" in capsys.readouterr().out


def test_a_kernel_older_than_6_2_is_named_and_not_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wizard, "load_platform", lambda source: FakeKernel(tiocsti=None, ptrace_scope=1))
    assert wizard.step_kernel_settings() == wizard.Outcome(False, "kernel older than 6.2")


def test_a_supported_system_passes_and_each_unsupported_one_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(wizard, "SUDO", str(tmp_path))
    monkeypatch.setattr(wizard, "SYSTEM_PYTHON", str(tmp_path))
    monkeypatch.setattr(wizard.os, "confstr", lambda name: "glibc 2.39")
    monkeypatch.setattr(wizard.os, "uname", lambda: SimpleNamespace(machine="x86_64"))
    assert wizard.unsupported_system() is None
    monkeypatch.setattr(wizard.os, "uname", lambda: SimpleNamespace(machine="riscv64"))
    assert "this is glibc 2.39 on riscv64" in str(wizard.unsupported_system())

    def musl(name: str) -> str:
        raise ValueError("unrecognized configuration name")

    monkeypatch.setattr(wizard.os, "confstr", musl)
    monkeypatch.setattr(wizard.os, "uname", lambda: SimpleNamespace(machine="aarch64"))
    assert "this is another C library on aarch64" in str(wizard.unsupported_system())
    monkeypatch.setattr(wizard, "SUDO", str(tmp_path / "missing" / "sudo"))
    assert wizard.unsupported_system() == f"envh's install needs {tmp_path / 'missing' / 'sudo'}, which this system doesn't have."
    monkeypatch.setattr(wizard.sys, "version_info", (3, 9, 18))
    assert str(wizard.unsupported_system()).startswith("The wizard needs Python 3.10 or newer")

