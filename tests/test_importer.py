import stat
from pathlib import Path

import pytest

from envh.client.transport import ClientError
from envh.tools.importer import (
    apply_renames,
    backup_originals,
    build_plan,
    build_presets,
    default_backup_root,
    discover,
    looks_secret,
    parse_env_text,
    review_classification,
    review_names,
    review_presets,
    rewrite_text,
    show_plan,
    slugify_header,
    suggest_secret_names,
    unquote,
    write_rewrites,
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
        0: "NEWS_BOT_GITHUB_TOKEN",
        2: "NEWS_BOT_DATABASE_URL",
        5: "NEWS_BOT_TEAM_OPENROUTER_API_KEY",
        6: "NEWS_BOT_TEAM_ASKNEWS_API_KEY",
        9: "NEWS_BOT_PERSONAL_OPENROUTER_API_KEY",
        10: "NEWS_BOT_PERSONAL_ASKNEWS_API_KEY",
    }
    presets = build_presets([parsed], decisions)
    base = {"GITHUB_TOKEN": "NEWS_BOT_GITHUB_TOKEN", "DATABASE_URL": "NEWS_BOT_DATABASE_URL"}
    assert presets == {
        "news-bot-team": {"env": {**base, "OPENROUTER_API_KEY": "NEWS_BOT_TEAM_OPENROUTER_API_KEY", "ASKNEWS_API_KEY": "NEWS_BOT_TEAM_ASKNEWS_API_KEY"}},
        "news-bot-personal": {"env": {**base, "OPENROUTER_API_KEY": "NEWS_BOT_PERSONAL_OPENROUTER_API_KEY", "ASKNEWS_API_KEY": "NEWS_BOT_PERSONAL_ASKNEWS_API_KEY"}},
    }
    rewritten = rewrite_text(parsed, decisions, presets)
    assert "LOG_LEVEL=info" in rewritten
    assert "# Team Mode" in rewritten and "# Personal Mode" in rewritten
    assert "sk-or" not in rewritten and "gh-token" not in rewritten and "pw@localhost" not in rewritten
    assert "# OPENROUTER_API_KEY -> envh secret NEWS_BOT_TEAM_OPENROUTER_API_KEY (presets: news-bot-team)" in rewritten
    assert "# OPENROUTER_API_KEY -> envh secret NEWS_BOT_PERSONAL_OPENROUTER_API_KEY (presets: news-bot-personal)" in rewritten
    assert rewritten.endswith("\n")
    plan = build_plan([parsed], decisions, presets)
    assert set(plan.secrets) == set(by_line.values())
    assert plan.secrets["NEWS_BOT_PERSONAL_ASKNEWS_API_KEY"] == "an-personal-1111111"


def test_cross_repo_conflicts_and_dedupe() -> None:
    first = parse_env_text(Path("/code/alpha/.env"), "OPENAI_API_KEY=sk-same\nSLACK_TOKEN=xoxb-alpha\n")
    second = parse_env_text(Path("/code/beta/.env"), "OPENAI_API_KEY=sk-same\nSLACK_TOKEN=xoxb-beta\n")
    flags = {**flags_for(first), **flags_for(second)}
    decisions = suggest_secret_names([first, second], flags)
    names = [(d.file.repo, d.assignment.name, d.secret_name) for d in decisions]
    assert names == [
        ("alpha", "OPENAI_API_KEY", "ALPHA_OPENAI_API_KEY"),
        ("alpha", "SLACK_TOKEN", "ALPHA_SLACK_TOKEN"),
        ("beta", "OPENAI_API_KEY", "ALPHA_OPENAI_API_KEY"),
        ("beta", "SLACK_TOKEN", "BETA_SLACK_TOKEN"),
    ]
    presets = build_presets([first, second], decisions)
    assert presets["alpha"]["env"] == {"OPENAI_API_KEY": "ALPHA_OPENAI_API_KEY", "SLACK_TOKEN": "ALPHA_SLACK_TOKEN"}
    assert presets["beta"]["env"] == {"OPENAI_API_KEY": "ALPHA_OPENAI_API_KEY", "SLACK_TOKEN": "BETA_SLACK_TOKEN"}


def test_a_project_folder_that_cannot_prefix_a_name_is_left_out() -> None:
    parsed = parse_env_text(Path("/code/2024-bot/.env"), "OPENAI_API_KEY=sk-1\n")
    assert suggest_secret_names([parsed], flags_for(parsed))[0].secret_name == "OPENAI_API_KEY"


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
    assert decisions[0].secret_name == "X_SETTINGS_FOR_THE_BOT_OPENAI_API_KEY"
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


def test_write_rewrites_follows_symlinks(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    real = shared / ".env"
    real.write_text("SLACK_TOKEN=xoxb-real-value-123\nLOG_LEVEL=info\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / ".env"
    link.symlink_to(real)
    parsed = parse_env_text(link, link.read_text())
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    plan = build_plan([parsed], decisions, build_presets([parsed], decisions))
    said: list[str] = []
    write_rewrites(plan, said.append)
    assert link.is_symlink() and "xoxb" not in real.read_text() and "LOG_LEVEL=info" in real.read_text()
    assert not list(shared.glob("*.envh-tmp")) and any("symlink" in line for line in said)


def test_secrets_the_importer_cannot_read_exactly_are_refused() -> None:
    text = 'PASSWORD="pa\\"ss"\nPRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"\n'
    parsed = parse_env_text(Path("/code/bot/.env"), text)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    with pytest.raises(ClientError, match=r"\.env:1: PASSWORD uses backslash escapes"):
        build_plan([parsed], decisions, {})
    with pytest.raises(ClientError, match=r"\.env:2: PRIVATE_KEY has a quoted value that does not close"):
        build_plan([parsed], decisions[1:], {})


def test_config_lines_with_escapes_do_not_block_an_import() -> None:
    parsed = parse_env_text(Path("/code/bot/.env"), 'LOG_FORMAT="%(message)s\\t%(levelname)s"\nAPI_KEY=\'raw\\value\'\n')
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    plan = build_plan([parsed], decisions, {})
    assert plan.secrets == {"BOT_API_KEY": "raw\\value"}


def test_review_names_insists_on_valid_vault_names() -> None:
    parsed = parse_env_text(Path("/code/bot/.env"), "_PRIVATE_TOKEN=tok-1234567890\n")
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    assert decisions[0].secret_name == "BOT_PRIVATE_TOKEN"
    decisions[0].secret_name = "_PRIVATE_TOKEN"
    answers = iter(["", "_PRIVATE_TOKEN=private_token"])
    said: list[str] = []
    review_names(decisions, lambda prompt: next(answers), said.append)
    assert decisions[0].secret_name == "PRIVATE_TOKEN"
    assert any("cannot be vault names" in line and "_PRIVATE_TOKEN" in line for line in said)


def test_names_the_vault_chose_reach_presets_and_files(tmp_path: Path) -> None:
    env_path = tmp_path / "news-bot" / ".env"
    env_path.parent.mkdir()
    env_path.write_text("OPENAI_API_KEY=sk-newsbot-0000000000\n")
    parsed = parse_env_text(env_path, env_path.read_text())
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    plan = build_plan([parsed], decisions, build_presets([parsed], decisions))
    apply_renames(plan, {"NEWS_BOT_OPENAI_API_KEY": "NEWS_BOT_OPENAI_API_KEY_2"})
    assert plan.presets["news-bot"]["env"] == {"OPENAI_API_KEY": "NEWS_BOT_OPENAI_API_KEY_2"}
    assert plan.rewrites[env_path] == "# OPENAI_API_KEY -> envh secret NEWS_BOT_OPENAI_API_KEY_2 (presets: news-bot)\n"


def test_the_plan_shows_the_new_lines_and_never_a_value() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    plan = build_plan([parsed], decisions, build_presets([parsed], decisions))
    said: list[str] = []
    show_plan(plan, [parsed], Path("/backups"), said.append)
    output = "\n".join(said)
    for original in ("gh-token-1234567890", "pw@localhost", "sk-or-team-000000", "an-team-000000", "sk-or-personal-1111111", "an-personal-1111111", "LOG_LEVEL=info"):
        assert original not in output
    assert "line 1    # GITHUB_TOKEN -> envh secret NEWS_BOT_GITHUB_TOKEN (presets: news-bot-personal, news-bot-team)" in output
    assert "line 11   # ASKNEWS_API_KEY -> envh secret NEWS_BOT_PERSONAL_ASKNEWS_API_KEY (presets: news-bot-personal)" in output


def test_presets_are_dropped_then_renamed_by_their_listed_names() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    answers = iter(["nope", "news-bot-personal", "news-bot-team=Bad Name", "news-bot-team=news"])
    said: list[str] = []
    presets = review_presets(build_presets([parsed], decisions), [parsed], lambda prompt: next(answers), said.append)
    assert list(presets) == ["news"]
    assert "  no preset named nope; use the names listed above" in said
    assert "  Bad Name: preset names use lowercase letters, digits, and . _ -" in said
    assert said[-1] == "  presets now: news"


def test_renaming_a_preset_onto_a_taken_name_is_refused() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    answers = iter(["", "news-bot-team=news-bot-personal", ""])
    said: list[str] = []
    presets = review_presets(build_presets([parsed], decisions), [parsed], lambda prompt: next(answers), said.append)
    assert list(presets) == ["news-bot-team", "news-bot-personal"]
    assert "  news-bot-personal is already taken" in said


def test_names_typed_twice_count_once_at_every_prompt() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    flags = review_classification([parsed], lambda prompt: "LOG_LEVEL, LOG_LEVEL", lambda line: None)
    assert flags[(parsed.path, 1)] is True
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    answers = iter(["news-bot-personal, news-bot-personal", ""])
    presets = review_presets(build_presets([parsed], decisions), [parsed], lambda prompt: next(answers), lambda line: None)
    assert list(presets) == ["news-bot-team"]


def test_one_preset_renamed_two_ways_is_refused() -> None:
    parsed = parse_env_text(Path("/code/news-bot/.env"), NEWS_BOT_ENV)
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    answers = iter(["", "news-bot-team=work, news-bot-team=home", "news-bot-team=work"])
    said: list[str] = []
    presets = review_presets(build_presets([parsed], decisions), [parsed], lambda prompt: next(answers), said.append)
    assert list(presets) == ["work", "news-bot-personal"]
    assert "  news-bot-team is renamed twice; give it one new name" in said


def test_a_comment_holding_a_key_is_never_a_group_name() -> None:
    text = "# Team Mode\nTEAM_TOKEN=fake-team-1234567890\n# old key: sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA\nOPENAI_API_KEY=fake-value-123456789\n# postgres://user:fakepass123@db/x\nDB_PASSWORD=fake-db-1234567890\n"
    parsed = parse_env_text(Path("/code/myproj/.env"), text)
    assert parsed.groups == ["Team Mode"]
    decisions = suggest_secret_names([parsed], flags_for(parsed))
    names = [decision.secret_name for decision in decisions]
    assert names == ["MYPROJ_TEAM_TEAM_TOKEN", "MYPROJ_TEAM_OPENAI_API_KEY", "MYPROJ_TEAM_DB_PASSWORD"]
    said: list[str] = []
    review_classification([parsed], lambda prompt: "", said.append)
    assert not any("sk-proj" in line or "fakepass" in line for line in said)
