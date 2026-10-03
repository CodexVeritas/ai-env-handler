import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_wizard", ROOT / "scripts" / "setup_wizard.py")
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

HOOK_COMMAND = "python3 /home/alice/.claude/hooks/envh_ask.py"


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


def test_settings_update_backs_up_writes_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model": "opus", "note": "café"}\n')
    settings_path.chmod(0o600)
    answer_inputs(monkeypatch, "y")
    assert wizard.offer_settings_hook(settings_path, HOOK_COMMAND) == "settings hook added"
    saved = json.loads(settings_path.read_text())
    assert saved["note"] == "café"
    assert wizard.envh_hook_commands(saved) == [HOOK_COMMAND]
    assert settings_path.stat().st_mode & 0o777 == 0o600
    backups = list(tmp_path.glob("settings.json.before-envh-*"))
    assert [backup.read_text() for backup in backups] == ['{"model": "opus", "note": "café"}\n']
    assert wizard.offer_settings_hook(settings_path, HOOK_COMMAND) == "settings hook already in place"


def test_declining_the_settings_update_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{}\n")
    answer_inputs(monkeypatch, "")
    assert wizard.offer_settings_hook(settings_path, HOOK_COMMAND) == "settings skipped"
    assert settings_path.read_text() == "{}\n"
    assert list(tmp_path.iterdir()) == [settings_path]


def test_invalid_settings_json_stops_with_a_message(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{not json")
    with pytest.raises(SystemExit, match="Fix the file"):
        wizard.offer_settings_hook(settings_path, HOOK_COMMAND)


@pytest.mark.parametrize(
    ("current", "expected"),
    [("", "snippet\n"), ("# Mine\n", "# Mine\n\nsnippet\n"), ("# Mine", "# Mine\n\nsnippet\n")],
)
def test_claude_md_snippet_is_appended_after_one_blank_line(current: str, expected: str) -> None:
    assert wizard.claude_md_with_snippet(current, "snippet\n") == expected


def test_claude_md_marker_is_in_the_snippet_so_reruns_skip_it(tmp_path: Path) -> None:
    snippet = (ROOT / "claude" / "CLAUDE.snippet.md").read_text()
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Mine\n\n" + snippet)
    assert wizard.offer_claude_md(claude_md, snippet) == "CLAUDE.md already in place"


def test_copy_creates_then_reports_up_to_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = ROOT / "claude" / "skills" / "envh" / "SKILL.md"
    destination = tmp_path / "skills" / "envh" / "SKILL.md"
    answer_inputs(monkeypatch, "y")
    assert wizard.offer_copy("skill", source, destination) == "skill installed"
    assert destination.read_text() == source.read_text()
    assert wizard.offer_copy("skill", source, destination) == "skill already in place"


def test_quit_answer_stops_the_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    answer_inputs(monkeypatch, "maybe", "q")
    with pytest.raises(wizard.Quit):
        wizard.ask_yes("Proceed?")


def test_only_the_build_files_are_staged_for_root(tmp_path: Path) -> None:
    wizard.stage_build_files(tmp_path / "staging")
    assert sorted(path.name for path in (tmp_path / "staging").iterdir()) == ["LICENSE", "README.md", "pyproject.toml", "src", "uv.lock"]
    assert not list((tmp_path / "staging").rglob("__pycache__"))


def test_root_half_refuses_to_run_without_sudo() -> None:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "setup_wizard.py"), "--install-as-root", "--source", "test"], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 1
    assert "root half" in result.stderr


def test_root_runs_the_wizard_with_the_system_python_and_sudo_by_absolute_path() -> None:
    command = wizard.root_install_command("abc123 from /repo", ["--dry-run"])
    assert command[:2] == ["/usr/bin/sudo", "/usr/bin/python3"]
    assert command[3:] == ["--install-as-root", "--source", "abc123 from /repo", "--dry-run"]
