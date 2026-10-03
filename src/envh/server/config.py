"""Policy files: config.yaml (defaults and per-secret policy) and presets.yaml (variable to secret mappings)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from envh.server.durations import MAX_SESSION, DurationError, format_duration, parse_duration

CONFIG_FILE = "config.yaml"
PRESETS_FILE = "presets.yaml"
APPROVALS = ("session", "per-run")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PRESET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

CONFIG_TEMPLATE = """# envh policy. Secrets not listed here get the defaults.
defaults:
  approval: session      # session: one console approval opens a session | per-run: ask on every run
  max_session: 1h        # longest session that may include a secret (hard cap 24h)
secrets: {}
#  DATABASE_URL: { approval: per-run }
#  OPENAI_API_KEY: { max_session: 8h }
"""

PRESETS_TEMPLATE = "presets: {}\n"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class SecretPolicy:
    name: str
    approval: str
    max_session: timedelta


@dataclass(frozen=True)
class PresetVar:
    var: str
    secret: str
    approval: str | None


@dataclass(frozen=True)
class Preset:
    name: str
    max_session: timedelta | None
    env: dict[str, PresetVar]


@dataclass(frozen=True)
class Defaults:
    approval: str
    max_session: timedelta


@dataclass
class Config:
    defaults: Defaults
    secret_policies: dict[str, SecretPolicy]
    presets: dict[str, Preset]
    presets_raw: dict[str, Any]

    def policy_for(self, secret: str) -> SecretPolicy:
        policy = self.secret_policies.get(secret)
        if policy is not None:
            return policy
        return SecretPolicy(name=secret, approval=self.defaults.approval, max_session=self.defaults.max_session)

    def effective_policy(self, preset_var: PresetVar) -> SecretPolicy:
        policy = self.policy_for(preset_var.secret)
        if preset_var.approval is None:
            return policy
        return SecretPolicy(name=policy.name, approval=preset_var.approval, max_session=policy.max_session)


def _expect_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(mapping) - set(allowed))
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}")


def _parse_approval(value: Any, where: str) -> str:
    if value not in APPROVALS:
        raise ConfigError(f"{where}: approval must be one of {', '.join(APPROVALS)}, got {value!r}")
    return value


def _parse_session_duration(value: Any, where: str) -> timedelta:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: max_session must be a string like 2h, got {value!r}")
    try:
        duration = parse_duration(value)
    except DurationError as error:
        raise ConfigError(f"{where}: {error}") from error
    if duration > MAX_SESSION:
        raise ConfigError(f"{where}: max_session {value} exceeds the hard cap of {format_duration(MAX_SESSION)}")
    return duration


def parse_config(text: str) -> tuple[Defaults, dict[str, SecretPolicy]]:
    document = _expect_mapping(yaml.safe_load(text), CONFIG_FILE)
    _reject_unknown(document, ("defaults", "secrets"), CONFIG_FILE)
    raw_defaults = _expect_mapping(document.get("defaults"), f"{CONFIG_FILE}: defaults")
    _reject_unknown(raw_defaults, ("approval", "max_session"), f"{CONFIG_FILE}: defaults")
    defaults = Defaults(
        approval=_parse_approval(raw_defaults.get("approval", "session"), f"{CONFIG_FILE}: defaults"),
        max_session=_parse_session_duration(raw_defaults.get("max_session", "1h"), f"{CONFIG_FILE}: defaults"),
    )
    policies: dict[str, SecretPolicy] = {}
    for name, raw in _expect_mapping(document.get("secrets"), f"{CONFIG_FILE}: secrets").items():
        where = f"{CONFIG_FILE}: secrets.{name}"
        if not isinstance(name, str) or not SECRET_NAME.match(name):
            raise ConfigError(f"{where}: secret names must be UPPER_CASE identifiers")
        fields = _expect_mapping(raw, where)
        _reject_unknown(fields, ("approval", "max_session"), where)
        policies[name] = SecretPolicy(
            name=name,
            approval=_parse_approval(fields.get("approval", defaults.approval), where),
            max_session=_parse_session_duration(fields.get("max_session", format_duration(defaults.max_session)), where),
        )
    return defaults, policies


def parse_presets(text: str, known_secrets: set[str] | None) -> tuple[dict[str, Preset], dict[str, Any]]:
    document = _expect_mapping(yaml.safe_load(text), PRESETS_FILE)
    _reject_unknown(document, ("presets",), PRESETS_FILE)
    raw_presets = _expect_mapping(document.get("presets"), f"{PRESETS_FILE}: presets")
    presets: dict[str, Preset] = {}
    for name, raw in raw_presets.items():
        presets[name] = parse_preset(name, raw, known_secrets)
    return presets, raw_presets


def parse_preset(name: Any, raw: Any, known_secrets: set[str] | None) -> Preset:
    where = f"preset {name!r}"
    if not isinstance(name, str) or not PRESET_NAME.match(name):
        raise ConfigError(f"{where}: preset names are lowercase, like forecasting-bot or auto-questions.minibench")
    fields = _expect_mapping(raw, where)
    _reject_unknown(fields, ("max_session", "env"), where)
    max_session = None
    if "max_session" in fields:
        max_session = _parse_session_duration(fields["max_session"], where)
    raw_env = _expect_mapping(fields.get("env"), f"{where}: env")
    if not raw_env:
        raise ConfigError(f"{where}: env must map at least one variable to a secret")
    env: dict[str, PresetVar] = {}
    for var, spec in raw_env.items():
        var_where = f"{where}: env.{var}"
        if not isinstance(var, str) or not VAR_NAME.match(var):
            raise ConfigError(f"{var_where}: environment variable names must be identifiers")
        if isinstance(spec, str):
            secret, approval = spec, None
        else:
            spec_fields = _expect_mapping(spec, var_where)
            _reject_unknown(spec_fields, ("secret", "approval"), var_where)
            if "secret" not in spec_fields:
                raise ConfigError(f"{var_where}: missing secret")
            secret = spec_fields["secret"]
            approval = _parse_approval(spec_fields["approval"], var_where) if "approval" in spec_fields else None
        if not isinstance(secret, str) or not SECRET_NAME.match(secret):
            raise ConfigError(f"{var_where}: secret names must be UPPER_CASE identifiers, got {secret!r}")
        if known_secrets is not None and secret not in known_secrets:
            raise ConfigError(f"{var_where}: secret {secret} is not in the vault")
        env[var] = PresetVar(var=var, secret=secret, approval=approval)
    return Preset(name=name, max_session=max_session, env=env)


def dump_presets(raw_presets: dict[str, Any]) -> str:
    return yaml.safe_dump({"presets": raw_presets}, sort_keys=True, default_flow_style=False)


def load_config(data_dir: Path, known_secrets: set[str] | None) -> Config:
    config_path = data_dir / CONFIG_FILE
    presets_path = data_dir / PRESETS_FILE
    if not config_path.exists():
        raise ConfigError(f"{config_path} does not exist; run `envh init`")
    defaults, policies = parse_config(config_path.read_text())
    presets_text = presets_path.read_text() if presets_path.exists() else PRESETS_TEMPLATE
    presets, raw = parse_presets(presets_text, known_secrets)
    return Config(defaults=defaults, secret_policies=policies, presets=presets, presets_raw=raw)
