from datetime import timedelta
from pathlib import Path

import pytest

from envh.server.config import (
    CONFIG_TEMPLATE,
    PRESETS_TEMPLATE,
    ConfigError,
    PresetVar,
    dump_presets,
    load_config,
    parse_config,
    parse_presets,
)


def test_template_parses_with_defaults() -> None:
    defaults, policies = parse_config(CONFIG_TEMPLATE)
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
    defaults, policies = parse_config(text)
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
    ],
)
def test_config_rejects(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)


def test_presets_short_and_long_form() -> None:
    text = """
presets:
  minibench:
    max_session: 3h
    env:
      OPENROUTER_API_KEY: MINIBENCH_OPENROUTER_KEY
      METACULUS_TOKEN: {secret: METACULUS_TOKEN, approval: per-run}
"""
    presets, raw = parse_presets(text, {"MINIBENCH_OPENROUTER_KEY", "METACULUS_TOKEN"})
    preset = presets["minibench"]
    assert preset.max_session == timedelta(hours=3)
    assert preset.env["OPENROUTER_API_KEY"] == PresetVar("OPENROUTER_API_KEY", "MINIBENCH_OPENROUTER_KEY", None)
    assert preset.env["METACULUS_TOKEN"].approval == "per-run"
    assert "minibench" in raw


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


def test_policy_resolution(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("defaults: {approval: session, max_session: 1h}\nsecrets: {DB: {approval: per-run}}\n")
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
