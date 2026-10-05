"""Readable summaries for the console: who asked, how a key is approved, presets as editable drafts, and what a change
to presets or settings does."""

from __future__ import annotations

import pwd
from dataclasses import dataclass
from typing import Any

from envh.core.config import Config, SecretEntry, Settings
from envh.core.durations import format_duration
from envh.server.console.tui import GREEN, RED, YELLOW, Line, Span

CHANGE_STYLES = {"+": GREEN, "-": RED, "~": YELLOW}


@dataclass(frozen=True)
class VarDraft:
    secret: str
    approval: str | None = None

    def text(self, var: str) -> str:
        return f"{var} ← {self.secret}" + (f" · {self.approval}" if self.approval else "")


@dataclass(frozen=True)
class PresetDraft:
    env: dict[str, VarDraft]
    max_session: str | None = None


def login_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def approval_text(config: Config, name: str) -> str:
    policy = config.policy_for(name)
    return "per-run" if policy.approval == "per-run" else f"session · {format_duration(policy.max_session)}"


def rule_text(entry: SecretEntry) -> str | None:
    """A key's own approval rule in a few words, or None when it has none."""
    parts = [entry.approval] if entry.approval else []
    if entry.max_session is not None:
        parts.append(f"up to {format_duration(entry.max_session)}")
    return " · ".join(parts) or None


def preset_drafts(raw: dict[str, Any]) -> dict[str, PresetDraft]:
    drafts = {}
    for name, preset in raw.items():
        env = {}
        for var, entry in (preset.get("env") or {}).items():
            env[var] = VarDraft(entry) if isinstance(entry, str) else VarDraft(entry["secret"], entry.get("approval"))
        drafts[name] = PresetDraft(env=env, max_session=preset.get("max_session"))
    return drafts


def presets_raw(drafts: dict[str, PresetDraft]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for name, draft in drafts.items():
        env = {var: entry.secret if entry.approval is None else {"secret": entry.secret, "approval": entry.approval} for var, entry in draft.env.items()}
        raw[name] = {"env": env, **({"max_session": draft.max_session} if draft.max_session else {})}
    return raw


def change(sign: str, text: str, indent: int = 0) -> Line:
    return [Span(" " * indent + sign + " ", CHANGE_STYLES[sign]), Span(text)]


def preset_changes(before: dict[str, PresetDraft], after: dict[str, PresetDraft], renamed: dict[str, str] | None = None) -> list[Line]:
    """What turning the presets before into after does, preset by preset. renamed maps new preset names to old ones."""
    renamed = renamed or {}
    lines: list[Line] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new or (new is None and name in renamed.values()):
            continue
        if new is None:
            lines.append(change("-", f"{name} (removed)"))
        elif old is None:
            lines.append(change("+", f"{name} (renamed from {renamed[name]})" if name in renamed else f"{name} (new)"))
            lines.extend(change("+", entry.text(var), 4) for var, entry in sorted(new.env.items()))
            if new.max_session:
                lines.append(change("+", f"longest session {new.max_session}", 4))
        else:
            lines.append(change("~", name))
            if old.max_session != new.max_session:
                lines.append(change("~", f"longest session {old.max_session or 'not set'} → {new.max_session or 'not set'}", 4))
            for var in sorted(set(old.env) | set(new.env)):
                before_var, after_var = old.env.get(var), new.env.get(var)
                if before_var == after_var:
                    continue
                if before_var is None:
                    lines.append(change("+", after_var.text(var), 4))
                elif after_var is None:
                    lines.append(change("-", before_var.text(var), 4))
                else:
                    lines.append(change("~", f"{after_var.text(var)} (was {before_var.text(var).split(' ← ', 1)[1]})", 4))
    return lines


def settings_changes(before: Settings, after: Settings) -> list[Line]:
    lines: list[Line] = []
    if before.notify != after.notify:
        lines.append(change("~", f"Notifications: {'on' if before.notify else 'off'} → {'on' if after.notify else 'off'}"))
    if before.users != after.users:
        lines.append(change("~", f"Allowed users: {', '.join(before.users)} → {', '.join(after.users)}"))
    if before.defaults.approval != after.defaults.approval:
        lines.append(change("~", f"Default approval: {before.defaults.approval} → {after.defaults.approval}"))
    if before.defaults.max_session != after.defaults.max_session:
        lines.append(change("~", f"Default longest session: {format_duration(before.defaults.max_session)} → {format_duration(after.defaults.max_session)}"))
    for name in sorted(set(before.secrets) | set(after.secrets)):
        old, new = before.secrets.get(name, SecretEntry()), after.secrets.get(name, SecretEntry())
        old_rule, new_rule = rule_text(old), rule_text(new)
        if old_rule != new_rule:
            if old_rule is None:
                lines.append(change("+", f"Rule for {name}: {new_rule}"))
            elif new_rule is None:
                lines.append(change("-", f"Rule for {name}; it follows the defaults again"))
            else:
                lines.append(change("~", f"Rule for {name}: {old_rule} → {new_rule}"))
        if old.description != new.description:
            if new.description is None:
                lines.append(change("-", f"Description of {name}"))
            else:
                lines.append(change("+" if old.description is None else "~", f'Description of {name}: "{new.description}"'))
    return lines


def merge(base: dict[str, Any], mine: dict[str, Any], theirs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Three-way merge of mine and theirs, two edits of base: each entry takes whichever side changed it. An entry both
    changed differently is a conflict and is left out."""
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    for name in sorted(set(base) | set(mine) | set(theirs), key=str):
        original, ours, other = base.get(name), mine.get(name), theirs.get(name)
        if ours == original:
            value = other
        elif other in (original, ours):
            value = ours
        else:
            conflicts.append(name)
            continue
        if value is not None:
            merged[name] = value
    return merged, conflicts


def settings_fields(settings: Settings) -> dict[str, Any]:
    return {"users": settings.users, "notify": settings.notify, "defaults": settings.defaults, **{f"key {name}": entry for name, entry in settings.secrets.items()}}


def settings_from_fields(fields: dict[str, Any]) -> Settings:
    secrets = {name.removeprefix("key "): entry for name, entry in fields.items() if name.startswith("key ")}
    return Settings(users=fields["users"], notify=fields["notify"], defaults=fields["defaults"], secrets=secrets)
