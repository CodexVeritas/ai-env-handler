import getpass
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from envh.core.config import (
    render_config_template,
    PRESETS_TEMPLATE,
    ConfigError,
    Defaults,
    PresetVar,
    SecretEntry,
    Settings,
    dump_presets,
    load_config,
    parse_config,
    parse_config_for_broker,
    parse_notify,
    parse_presets,
    parse_settings,
    render_config,
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


def test_a_yaml_alias_bomb_in_a_preset_is_a_bounded_error_not_a_huge_string() -> None:
    import time

    # YAML aliases let a tiny document describe a width**depth object graph. safe_load keeps those as
    # shared references (cheap), but repr() of them would materialise the full expansion — so an error
    # message that repr()'d the parsed value exploded to many megabytes and wedged/killed the broker.
    def inner(level: int, width: int = 6) -> str:
        if level == 0:
            return '&a0 "x"'
        return f"&a{level} [" + inner(level - 1, width) + f",*a{level - 1}" * (width - 1) + "]"

    bomb = f"presets:\n  p:\n    max_session: {inner(7)}\n    env:\n      X: A\n"
    start = time.monotonic()
    with pytest.raises(ConfigError) as excinfo:
        parse_presets(bomb, {"A"})
    assert time.monotonic() - start < 5
    assert len(str(excinfo.value)) < 500  # describes "a list", never the expanded graph


def test_an_absurd_duration_in_a_preset_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        parse_presets("presets: {p: {max_session: '" + "9" * 500 + "h', env: {A: A}}}", {"A"})


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


def test_settings_are_written_with_comments_and_read_back_exactly() -> None:
    settings = Settings(
        users=(getpass.getuser(),),
        notify=False,
        defaults=Defaults(approval="per-run", max_session=timedelta(hours=2)),
        secrets={
            "DATABASE_URL": SecretEntry(approval="per-run"),
            "OPENAI_API_KEY": SecretEntry(approval="session", max_session=timedelta(hours=8), description='Personal "OpenAI" key — news bot'),
            "UNUSED_KEY": SecretEntry(),
        },
    )
    text = render_config(settings)
    assert parse_settings(text) == replace(settings, secrets={name: entry for name, entry in settings.secrets.items() if name != "UNUSED_KEY"})
    assert "# logins whose scripts and agents may ask for keys" in text and "UNUSED_KEY" not in text
    _, policies, _, _ = parse_config(text)
    assert policies["OPENAI_API_KEY"].max_session == timedelta(hours=8) and policies["DATABASE_URL"].max_session == timedelta(hours=2)


def test_a_description_alone_sets_no_policy() -> None:
    text = f"users: [{getpass.getuser()}]\nsecrets: {{KEY: {{description: the prod key}}}}\n"
    _, policies, _, _ = parse_config(text)
    assert policies == {} and parse_settings(text).secrets["KEY"] == SecretEntry(description="the prod key")


@pytest.mark.parametrize("description", ["x" * 201, "two\nlines", "bell\a", "\u202eevil", 5])
def test_a_description_is_one_short_line_of_plain_text(description: object) -> None:
    with pytest.raises(ConfigError, match="description"):
        parse_config(f"users: [{getpass.getuser()}]\nsecrets: {{KEY: {{description: {json.dumps(description)}}}}}\n")


def test_notify_defaults_to_on_and_takes_only_true_or_false(tmp_path: Path) -> None:
    user = getpass.getuser()
    assert parse_notify(render_config_template(user)) is True
    assert parse_notify(f"users: [{user}]\n") is True
    assert parse_notify(f"users: [{user}]\nnotify: false\n") is False
    with pytest.raises(ConfigError, match="notify must be true or false"):
        parse_config(f"users: [{user}]\nnotify: sometimes\n")
    (tmp_path / "config.yaml").write_text(f"users: [{user}]\nnotify: false\n")
    assert load_config(tmp_path, set()).notify is False


@pytest.mark.parametrize("name", ["NO", "YES", "ON", "OFF", "TRUE", "FALSE", "NULL"])
def test_written_settings_quote_a_name_yaml_would_read_as_a_boolean_or_null(name: str) -> None:
    settings = Settings(users=(getpass.getuser(),), notify=True, defaults=Defaults("session", timedelta(hours=1)), secrets={name: SecretEntry(approval="per-run")})
    _, policies, _, _ = parse_config(render_config(settings))
    assert policies[name].approval == "per-run"
