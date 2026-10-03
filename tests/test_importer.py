import stat
from pathlib import Path

import pytest

from envh.client.transport import ClientError
from envh.tools.importer import (
    backup_originals,
    build_plan,
    build_presets,
    default_backup_root,
    discover,
    looks_secret,
    parse_env_text,
    rewrite_text,
    slugify_header,
    suggest_secret_names,
    unquote,
)

NEWS_BOT_ENV = """GITHUB_TOKEN=gh-token-1234567890
LOG_LEVEL=info
export DATABASE_URL="postgres://user:pw@localhost/db"

# Team Mode
OPENROUTER_API_KEY=sk-or-team-000000
ASKNEWS_API_KEY='an-team-000000'

# Personal Mode
# OPENROUTER_API_KEY=sk-or-personal-1111111
# ASKNEWS_API_KEY=an-personal-1111111
"""


def flags_for(parsed) -> dict[tuple[Path, int], bool]:
    return {(parsed.path, a.line_index): looks_secret(a.name, a.value) for a in parsed.assignments}


def test_parse_groups_and_commented_assignments() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    assert parsed.groups == ["Team Mode", "Personal Mode"]
    assert parsed.repo == "news-bot"
    names = [(a.name, a.active, a.group) for a in parsed.assignments]
    assert names == [
        ("GITHUB_TOKEN", True, None),
        ("LOG_LEVEL", True, None),
        ("DATABASE_URL", True, None),
        ("OPENROUTER_API_KEY", True, "Team Mode"),
        ("ASKNEWS_API_KEY", True, "Team Mode"),
        ("OPENROUTER_API_KEY", False, "Personal Mode"),
        ("ASKNEWS_API_KEY", False, "Personal Mode"),
    ]
    assert parsed.assignments[2].value == "postgres://user:pw@localhost/db"
    assert parsed.assignments[4].value == "an-team-000000"


def test_classification_and_unquote() -> None:
    assert looks_secret("OPENAI_API_KEY", "x") and looks_secret("DATABASE_URL", "postgres://u:p@h/db")
    assert not looks_secret("LOG_LEVEL", "info") and not looks_secret("BASE_URL", "https://example.com")
    assert unquote('"a b" # note') == "a b" and unquote("plain # note") == "plain" and unquote("x#y") == "x#y"


def test_header_slugs() -> None:
    assert slugify_header("Team Mode") == "team"
    assert slugify_header("Personal Mode") == "personal"
    assert slugify_header("Production keys") == "production"
    assert slugify_header("Mode") == "mode"


def test_names_presets_and_rewrite() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    by_line = {d.assignment.line_index: d.secret_name for d in decisions}
    assert by_line == {
        0: "GITHUB_TOKEN",
        2: "DATABASE_URL",
        5: "TEAM_OPENROUTER_API_KEY",
        6: "TEAM_ASKNEWS_API_KEY",
        9: "PERSONAL_OPENROUTER_API_KEY",
        10: "PERSONAL_ASKNEWS_API_KEY",
    }
    presets = build_presets([parsed], decisions)
    assert presets == {
        "news-bot-team": {"env": {"GITHUB_TOKEN": "GITHUB_TOKEN", "DATABASE_URL": "DATABASE_URL", "OPENROUTER_API_KEY": "TEAM_OPENROUTER_API_KEY", "ASKNEWS_API_KEY": "TEAM_ASKNEWS_API_KEY"}},
        "news-bot-personal": {"env": {"GITHUB_TOKEN": "GITHUB_TOKEN", "DATABASE_URL": "DATABASE_URL", "OPENROUTER_API_KEY": "PERSONAL_OPENROUTER_API_KEY", "ASKNEWS_API_KEY": "PERSONAL_ASKNEWS_API_KEY"}},
    }
    rewritten = rewrite_text(parsed, decisions, presets)
    assert "LOG_LEVEL=info" in rewritten
    assert "# Team Mode" in rewritten and "# Personal Mode" in rewritten
    assert "sk-or" not in rewritten and "gh-token" not in rewritten and "pw@localhost" not in rewritten
    assert "# OPENROUTER_API_KEY -> envh secret TEAM_OPENROUTER_API_KEY (presets: news-bot-team)" in rewritten
    assert "# OPENROUTER_API_KEY -> envh secret PERSONAL_OPENROUTER_API_KEY (presets: news-bot-personal)" in rewritten
    assert rewritten.endswith("\n")
    plan = build_plan([parsed], decisions, presets)
    assert set(plan.secrets) == set(by_line.values())
    assert plan.secrets["PERSONAL_ASKNEWS_API_KEY"] == "an-personal-1111111"


def test_cross_repo_conflicts_and_dedupe() -> None:
    first = parse_env_text(Path("/code/alpha/.env"), "OPENAI_API_KEY=sk-same\nSLACK_TOKEN=xoxb-alpha\n")
    second = parse_env_text(Path("/code/beta/.env"), "OPENAI_API_KEY=sk-same\nSLACK_TOKEN=xoxb-beta\n")
    flags = {**flags_for(first), **flags_for(second)}
    decisions = suggest_secret_names([first, second], flags)
    names = [(d.file.repo, d.assignment.name, d.secret_name) for d in decisions]
    assert names == [
        ("alpha", "OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("alpha", "SLACK_TOKEN", "SLACK_TOKEN"),
        ("beta", "OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("beta", "SLACK_TOKEN", "BETA_SLACK_TOKEN"),
    ]
    presets = build_presets([first, second], decisions)
    assert presets["alpha"]["env"] == {"OPENAI_API_KEY": "OPENAI_API_KEY", "SLACK_TOKEN": "SLACK_TOKEN"}
    assert presets["beta"]["env"] == {"OPENAI_API_KEY": "OPENAI_API_KEY", "SLACK_TOKEN": "BETA_SLACK_TOKEN"}


def test_discover(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".env").write_text("X=1\n")
    (tmp_path / "a" / ".env.example").write_text("X=\n")
    (tmp_path / "a" / ".env.local").write_text("Y=1\n")
    (tmp_path / "a" / "node_modules").mkdir()
    (tmp_path / "a" / "node_modules" / ".env").write_text("Z=1\n")
    deep = tmp_path / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / ".env").write_text("DEEP=1\n")
    found = discover([str(tmp_path)], depth=3)
    assert [path.relative_to(tmp_path).as_posix() for path in found] == ["a/.env", "a/.env.local"]
    assert discover([str(tmp_path / "a" / ".env")], depth=1) == [tmp_path / "a" / ".env"]


def test_generic_comments_do_not_become_groups() -> None:
    parsed = parse_env_text(Path("/code/x/.env"), "# Settings for the bot\nOPENAI_API_KEY=sk-1\n# nothing follows this comment\n")
    assert parsed.groups == ["Settings for the bot"]
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    assert decisions[0].secret_name == "SETTINGS_FOR_THE_BOT_OPENAI_API_KEY"
    assert list(build_presets([parsed], decisions)) == ["x-settings-for-the-bot"]


def test_envh_notes_are_not_group_headers_and_duplicate_names_rejected() -> None:
    text = "# OPENAI_API_KEY -> envh secret OPENAI_API_KEY (presets: x)\nLOG_LEVEL=info\nSLACK_TOKEN=xoxb-1\n"
    parsed = parse_env_text(Path("/code/x/.env"), text)
    assert parsed.groups == []
    first = parse_env_text(Path("/code/a/.env"), "KEY_ONE=value-one\nKEY_TWO=value-two\n")
    decisions = suggest_secret_names([first], flags_for(first))
    decisions[1].secret_name = decisions[0].secret_name
    with pytest.raises(ClientError, match="two different values"):
        build_plan([first], decisions, build_presets([first], decisions))


def test_backup_mirrors_each_original_in_a_private_folder(tmp_path: Path) -> None:
    env_file = tmp_path / "repo" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("OPENAI_API_KEY=sk-original\n")
    backup_dir = backup_originals([env_file], tmp_path / "backups", "20261003-120000")
    copy = backup_dir / env_file.relative_to("/")
    assert backup_dir == tmp_path / "backups" / "20261003-120000"
    assert copy.read_text() == "OPENAI_API_KEY=sk-original\n"
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700 and stat.S_IMODE(copy.stat().st_mode) == 0o600
    with pytest.raises(ClientError, match="could not back up the original files"):
        backup_originals([env_file], tmp_path / "backups", "20261003-120000")


def test_default_backup_root_follows_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_backup_root() == tmp_path / "state" / "envh" / "import-backups"
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_backup_root() == tmp_path / ".local" / "state" / "envh" / "import-backups"
