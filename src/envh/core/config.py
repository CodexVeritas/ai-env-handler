"""Policy files: config.yaml (defaults and per-key settings) and presets.yaml (variable to secret mappings)."""

from __future__ import annotations

import json
import pwd
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from envh.common import PRESET_NAME, SECRET_NAME, printable
from envh.core.durations import MAX_SESSION, DurationError, format_duration, parse_duration

CONFIG_FILE = "config.yaml"
PRESETS_FILE = "presets.yaml"
AUTO = "auto"
APPROVALS = ("session", "per-run")
KEY_APPROVALS = (AUTO, *APPROVALS)  # least strict first; auto is for a key's own rule only, never the defaults or a preset
SECRET_FIELDS = ("approval", "max_session", "description")
VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9_.-]+$")
DESCRIPTION_LIMIT = 200
COMMENT_COLUMN = 26
DEFAULT_MAX_SESSION = timedelta(hours=1)

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


@dataclass(frozen=True)
class SecretEntry:
    """A key's own settings in config.yaml; None follows the defaults (or, for the description, means there is none)."""

    approval: str | None = None
    max_session: timedelta | None = None
    description: str | None = None

    @property
    def empty(self) -> bool:
        return self == SecretEntry()


@dataclass(frozen=True)
class Settings:
    """config.yaml as the console edits it. Each key's entry keeps only the fields it sets, so the rest keeps following the
    defaults when they change."""

    users: tuple[str, ...]
    notify: bool
    defaults: Defaults
    secrets: dict[str, SecretEntry] = field(default_factory=dict)


@dataclass
class Config:
    defaults: Defaults
    secret_policies: dict[str, SecretPolicy]
    presets: dict[str, Preset]
    presets_raw: dict[str, Any]
    settings: Settings
    users: tuple[str, ...] = ()
    allowed_uids: frozenset[int] = frozenset()
    notify: bool = True

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

    def presets_using(self, secret: str) -> list[str]:
        return sorted(preset.name for preset in self.presets.values() if any(entry.secret == secret for entry in preset.env.values()))

    def description_for(self, secret: str) -> str | None:
        entry = self.settings.secrets.get(secret)
        return entry.description if entry is not None else None


def _load_yaml(text: str, where: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"{where}: not valid YAML: {' '.join(str(error).split())}") from error


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


def _parse_approval(value: Any, where: str, allowed: tuple[str, ...] = APPROVALS) -> str:
    if value not in allowed:
        raise ConfigError(f"{where}: approval must be one of {', '.join(allowed)}, got {value!r}")
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


def _parse_description(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: description must be text, got {value!r}")
    if len(value) > DESCRIPTION_LIMIT:
        raise ConfigError(f"{where}: description is longer than {DESCRIPTION_LIMIT} characters")
    if printable(value) != value:
        raise ConfigError(f"{where}: description must be one line of plain text")
    return value


def parse_users(value: Any) -> tuple[tuple[str, ...], frozenset[int]]:
    if value is None:
        return (), frozenset()
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{CONFIG_FILE}: users must be a list of login names")
    uids: set[int] = set()
    for name in value:
        try:
            uids.add(pwd.getpwnam(name).pw_uid)
        except KeyError as error:
            raise ConfigError(f"{CONFIG_FILE}: users: no such login name {name!r}") from error
    return tuple(value), frozenset(uids)


def parse_notify(text: str) -> bool:
    value = _expect_mapping(_load_yaml(text, CONFIG_FILE), CONFIG_FILE).get("notify", True)
    if not isinstance(value, bool):
        raise ConfigError(f"{CONFIG_FILE}: notify must be true or false, got {value!r}")
    return value


def parse_config(text: str) -> tuple[Defaults, dict[str, SecretPolicy], tuple[str, ...], frozenset[int]]:
    document = _expect_mapping(_load_yaml(text, CONFIG_FILE), CONFIG_FILE)
    _reject_unknown(document, ("users", "notify", "defaults", "secrets"), CONFIG_FILE)
    parse_notify(text)
    users, allowed_uids = parse_users(document.get("users"))
    raw_defaults = _expect_mapping(document.get("defaults"), f"{CONFIG_FILE}: defaults")
    _reject_unknown(raw_defaults, ("approval", "max_session"), f"{CONFIG_FILE}: defaults")
    defaults = Defaults(
        approval=_parse_approval(raw_defaults.get("approval", "session"), f"{CONFIG_FILE}: defaults"),
        max_session=_parse_session_duration(raw_defaults.get("max_session", format_duration(DEFAULT_MAX_SESSION)), f"{CONFIG_FILE}: defaults"),
    )
    policies: dict[str, SecretPolicy] = {}
    for name, raw in _expect_mapping(document.get("secrets"), f"{CONFIG_FILE}: secrets").items():
        where = f"{CONFIG_FILE}: secrets.{name}"
        if not isinstance(name, str) or not SECRET_NAME.match(name):
            raise ConfigError(f"{where}: secret names must be UPPER_CASE identifiers")
        fields = _expect_mapping(raw, where)
        _reject_unknown(fields, SECRET_FIELDS, where)
        if "description" in fields:
            _parse_description(fields["description"], where)
        approval = _parse_approval(fields.get("approval", defaults.approval), where, KEY_APPROVALS)
        max_session = _parse_session_duration(fields.get("max_session", format_duration(defaults.max_session)), where)
        if "approval" in fields or "max_session" in fields:
            policies[name] = SecretPolicy(name=name, approval=approval, max_session=max_session)
    return defaults, policies, users, allowed_uids


def parse_settings(text: str) -> Settings:
    """config.yaml, validated, with each key's entry as written: fields it leaves out stay None."""
    defaults, _, users, _ = parse_config(text)
    document = _expect_mapping(_load_yaml(text, CONFIG_FILE), CONFIG_FILE)
    secrets: dict[str, SecretEntry] = {}
    for name, raw in _expect_mapping(document.get("secrets"), f"{CONFIG_FILE}: secrets").items():
        fields = _expect_mapping(raw, f"{CONFIG_FILE}: secrets.{name}")
        entry = SecretEntry(
            approval=fields.get("approval"),
            max_session=parse_duration(fields["max_session"]) if "max_session" in fields else None,
            description=fields.get("description") or None,
        )
        if not entry.empty:
            secrets[name] = entry
    return Settings(users=users, notify=parse_notify(text), defaults=defaults, secrets=secrets)


def yaml_scalar(text: str) -> str:
    """text as a YAML scalar: plain where YAML reads it back unchanged (NO and NULL would be a boolean and null), else quoted."""
    if PLAIN_SCALAR.match(text) and yaml.safe_load(text) == text:
        return text
    return json.dumps(text)


def _commented(code: str, comment: str) -> str:
    return f"{code:<{COMMENT_COLUMN - 1}} # {comment}"


def render_config(settings: Settings) -> str:
    """config.yaml for these settings, with a comment on each option. Refuses settings it could not write exactly."""
    lines = [
        "# envh settings. Change them on the console with /settings; saving there rewrites this file.",
        _commented(f"users: [{', '.join(yaml_scalar(user) for user in settings.users)}]", "logins whose scripts and agents may ask for keys"),
        _commented(f"notify: {'true' if settings.notify else 'false'}", "notification, chime and console bell while a request waits"),
        _commented("defaults:", "for every key without its own setting below"),
        _commented(f"  approval: {settings.defaults.approval}", "session: one approval opens a session | per-run: ask on every run"),
        _commented(f"  max_session: {format_duration(settings.defaults.max_session)}", "longest session that may include a key; hard cap 24h"),
    ]
    entries = {name: entry for name, entry in sorted(settings.secrets.items()) if not entry.empty}
    if entries:
        lines.append(_commented("secrets:", "per key: approval, max_session, description; the rest follows the defaults"))
    else:
        lines.append(_commented("secrets: {}", 'per key, like DATABASE_URL: {approval: per-run, description: "..."}'))
    for name, entry in entries.items():
        fields = []
        if entry.approval is not None:
            fields.append(f"approval: {entry.approval}")
        if entry.max_session is not None:
            fields.append(f"max_session: {format_duration(entry.max_session)}")
        if entry.description is not None:
            fields.append(f"description: {json.dumps(entry.description, ensure_ascii=False)}")
        lines.append(f"  {yaml_scalar(name)}: {{{', '.join(fields)}}}")
    text = "\n".join(lines) + "\n"
    if parse_settings(text) != replace(settings, secrets=entries):
        raise ConfigError(f"{CONFIG_FILE}: these settings could not be written exactly")
    return text


def render_config_template(user: str) -> str:
    return render_config(Settings(users=(user,), notify=True, defaults=Defaults(approval="session", max_session=DEFAULT_MAX_SESSION)))


def parse_config_for_broker(text: str) -> tuple[Defaults, dict[str, SecretPolicy], tuple[str, ...], frozenset[int]]:
    """parse_config plus the rule the broker needs to start: at least one allowed user."""
    defaults, policies, users, allowed_uids = parse_config(text)
    if not allowed_uids:
        raise ConfigError(f"{CONFIG_FILE}: users is empty; list the login names allowed to talk to the broker, for example users: [alice]")
    return defaults, policies, users, allowed_uids


def parse_presets(text: str, known_secrets: set[str] | None) -> tuple[dict[str, Preset], dict[str, Any]]:
    document = _expect_mapping(_load_yaml(text, PRESETS_FILE), PRESETS_FILE)
    _reject_unknown(document, ("presets",), PRESETS_FILE)
    raw_presets = _expect_mapping(document.get("presets"), f"{PRESETS_FILE}: presets")
    presets: dict[str, Preset] = {}
    for name, raw in raw_presets.items():
        presets[name] = parse_preset(name, raw, known_secrets)
    return presets, raw_presets


def parse_preset(name: Any, raw: Any, known_secrets: set[str] | None) -> Preset:
    where = f"preset {name!r}"
    if not isinstance(name, str) or not PRESET_NAME.match(name):
        raise ConfigError(f"{where}: preset names are lowercase, like forecasting-bot or news-bot.team")
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
    presets_text = presets_path.read_text() if presets_path.exists() else PRESETS_TEMPLATE
    return config_from_text(config_path.read_text(), presets_text, known_secrets)


def config_from_text(config_text: str, presets_text: str, known_secrets: set[str] | None) -> Config:
    defaults, policies, users, allowed_uids = parse_config_for_broker(config_text)
    presets, raw = parse_presets(presets_text, known_secrets)
    return Config(
        defaults=defaults,
        secret_policies=policies,
        presets=presets,
        presets_raw=raw,
        users=users,
        allowed_uids=allowed_uids,
        notify=parse_notify(config_text),
        settings=parse_settings(config_text),
    )
