import importlib.util
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

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
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "setup_wizard.py"), "--install-as-root", "--source", "test"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert "root half" in result.stderr


def test_root_runs_the_wizard_with_the_system_python_and_sudo_by_absolute_path() -> None:
    command = wizard.root_install_command("abc123 from /repo", ["--ignore-preflight"])
    assert command[:2] == ["/usr/bin/sudo", "/usr/bin/python3"]
    assert command[3:] == ["--install-as-root", "--source", "abc123 from /repo", "--ignore-preflight"]


def test_the_system_checks_load_from_the_staged_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    wizard.stage_build_files(tmp_path / "staging")
    problems, warnings = wizard.system_checks(tmp_path / "staging" / "src", "nobody")
    assert isinstance(problems, list) and isinstance(warnings, list)
