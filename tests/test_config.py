import getpass
import os
from datetime import timedelta
from pathlib import Path

import pytest

from envh.core.config import (
    render_config_template,
    PRESETS_TEMPLATE,
    ConfigError,
    PresetVar,
    dump_presets,
    load_config,
    parse_config,
    parse_config_for_broker,
    parse_presets,
    renamed_in_config,
)


def test_template_parses_with_defaults() -> None:
    defaults, policies, users, uids = parse_config(render_config_template(getpass.getuser()))
    assert users == (getpass.getuser(),) and uids == frozenset({os.getuid()})
    assert defaults.approval == "session"
    assert defaults.max_session == timedelta(hours=1)
    assert policies == {}


def test_secret_overrides_and_defaults() -> None:
    text = """
defaults: {approval: session, max_session: 2h}
secrets:
  DATABASE_URL: {approval: per-run}
  OPENAI_API_KEY: {max_session: 8h}
"""
    defaults, policies, _, _ = parse_config(text)
    assert policies["DATABASE_URL"].approval == "per-run"
    assert policies["DATABASE_URL"].max_session == timedelta(hours=2)
    assert policies["OPENAI_API_KEY"].approval == "session"
    assert policies["OPENAI_API_KEY"].max_session == timedelta(hours=8)


@pytest.mark.parametrize(
    "text",
    [
        "defaults: {approval: sometimes}",
        "defaults: {max_session: 25h}",
        "defaults: {max_session: 90}",
        "defaults: {ttl: 5m}",
        "secrets: {lower_case: {}}",
        "secrets: {KEY: {mode: proxy}}",
        "secrets: {KEY: {max_session: 2d}}",
        "extra: 1",
        "- a list",
        "users: alice",
        "users: [no-such-login-name-xyz]",
    ],
)
def test_config_rejects(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)


def test_presets_short_and_long_form() -> None:
    text = """
presets:
  team:
    max_session: 3h
    env:
      OPENROUTER_API_KEY: TEAM_OPENROUTER_KEY
      GITHUB_TOKEN: {secret: GITHUB_TOKEN, approval: per-run}
"""
    presets, raw = parse_presets(text, {"TEAM_OPENROUTER_KEY", "GITHUB_TOKEN"})
    preset = presets["team"]
    assert preset.max_session == timedelta(hours=3)
    assert preset.env["OPENROUTER_API_KEY"] == PresetVar("OPENROUTER_API_KEY", "TEAM_OPENROUTER_KEY", None)
    assert preset.env["GITHUB_TOKEN"].approval == "per-run"
    assert "team" in raw


@pytest.mark.parametrize(
    "text",
    [
        "presets: {Bad Name: {env: {A: A}}}",
        "presets: {p: {env: {}}}",
        "presets: {p: {env: {'1bad': A}}}",
        "presets: {p: {env: {A: lower}}}",
        "presets: {p: {env: {A: {approval: per-run}}}}",
        "presets: {p: {env: {A: {secret: A, mode: x}}}}",
        "presets: {p: {ttl: 1h, env: {A: A}}}",
        "presets: {p: {max_session: 48h, env: {A: A}}}",
        "other: {}",
    ],
)
def test_presets_reject(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_presets(text, {"A"})


def test_presets_require_known_secret_when_names_given() -> None:
    text = "presets: {p: {env: {A: MISSING}}}"
    with pytest.raises(ConfigError, match="not in the vault"):
        parse_presets(text, {"A"})
    presets, _ = parse_presets(text, None)
    assert presets["p"].env["A"].secret == "MISSING"


def test_load_requires_users(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("defaults: {approval: session}\n")
    with pytest.raises(ConfigError, match="users is empty"):
        load_config(tmp_path, set())


def test_policy_resolution(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(f"users: [{getpass.getuser()}]\ndefaults: {{approval: session, max_session: 1h}}\nsecrets: {{DB: {{approval: per-run}}}}\n")
    (tmp_path / "presets.yaml").write_text("presets: {p: {env: {DATABASE_URL: DB, KEY: {secret: KEY, approval: per-run}}}}\n")
    config = load_config(tmp_path, {"DB", "KEY"})
    assert config.policy_for("UNLISTED").approval == "session"
    assert config.policy_for("DB").approval == "per-run"
    key_var = config.presets["p"].env["KEY"]
    assert config.effective_policy(key_var).approval == "per-run"
    assert config.effective_policy(config.presets["p"].env["DATABASE_URL"]).approval == "per-run"


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="envh init"):
        load_config(tmp_path, set())


def test_dump_presets_round_trip() -> None:
    _, raw = parse_presets("presets: {p: {max_session: 2h, env: {A: A}}}", {"A"})
    presets, _ = parse_presets(dump_presets(raw), {"A"})
    assert presets["p"].max_session == timedelta(hours=2)
    assert parse_presets(PRESETS_TEMPLATE, set()) == ({}, {})


@pytest.mark.parametrize("text", ["presets: {bad: [", "defaults: {approval: session"])
def test_yaml_syntax_errors_are_config_errors(text: str) -> None:
    with pytest.raises(ConfigError, match="not valid YAML"):
        if text.startswith("presets"):
            parse_presets(text, None)
        else:
            parse_config(text)


def test_parse_config_for_broker_requires_users() -> None:
    with pytest.raises(ConfigError, match="users is empty"):
        parse_config_for_broker("defaults: {approval: session}\n")
    assert parse_config_for_broker(render_config_template(getpass.getuser()))[3] == frozenset({os.getuid()})


def test_renamed_in_config_moves_the_policy_and_keeps_comments() -> None:
    text = f"users: [{getpass.getuser()}]\nsecrets:\n  # OLD_KEY is the prod key\n  OLD_KEY: {{ approval: per-run }}  # OLD_KEY note\n  OLD_KEY_2: {{ max_session: 2h }}\n"
    renamed = renamed_in_config(text, "OLD_KEY", "NEW_KEY")
    assert renamed == text.replace("  OLD_KEY: {", "  NEW_KEY: {")
    _, policies, _, _ = parse_config(renamed)
    assert policies["NEW_KEY"].approval == "per-run" and "OLD_KEY_2" in policies


def test_renamed_in_config_refuses_a_name_that_already_has_a_policy() -> None:
    text = f"users: [{getpass.getuser()}]\nsecrets: {{OLD_KEY: {{approval: per-run}}, NEW_KEY: {{max_session: 8h}}}}\n"
    with pytest.raises(ConfigError, match="already has a policy for NEW_KEY"):
        renamed_in_config(text, "OLD_KEY", "NEW_KEY")


@pytest.mark.parametrize("new", ["NO", "YES", "ON", "OFF", "TRUE", "FALSE", "NULL"])
def test_renamed_in_config_quotes_a_name_yaml_would_read_as_a_boolean_or_null(new: str) -> None:
    text = f"users: [{getpass.getuser()}]\nsecrets:\n  OLD_KEY: {{ approval: per-run }}\n"
    _, policies, _, _ = parse_config(renamed_in_config(text, "OLD_KEY", new))
    assert policies[new].approval == "per-run"
