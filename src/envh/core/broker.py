"""The broker: policy decisions, request lifecycle, and the side effects of approvals."""

from __future__ import annotations

import copy
import difflib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from envh.common import fingerprint
from envh.core.audit import Audit
from envh.core.config import (
    AUTO,
    CONFIG_FILE,
    KEY_APPROVALS,
    PRESETS_FILE,
    SECRET_NAME,
    VAR_NAME,
    Config,
    ConfigError,
    Preset,
    SecretPolicy,
    Settings,
    config_from_text,
    dump_presets,
    load_config,
    parse_presets,
    parse_settings,
    render_config,
)
from envh.core.durations import MAX_SESSION, format_duration
from envh.core.state import Provenance, Request, Session, StateTable
from envh.core.vault import MIN_PASSPHRASE_LENGTH, Vault, VaultError, move_into_place, stage_private_file, write_private_file


MAX_PRESET_BYTES = 64 * 1024


class RequestError(ValueError):
    """A request that cannot be granted as asked; the message is shown to the requester."""


def renamed_presets(presets: dict[str, Any], final_names: dict[str, str]) -> dict[str, Any]:
    """A copy of presets whose secret references follow final_names; malformed entries are left for validation to reject."""
    renamed = copy.deepcopy(presets)
    for preset in renamed.values():
        env = preset.get("env") if isinstance(preset, dict) else None
        if not isinstance(env, dict):
            continue
        for var, entry in env.items():
            if isinstance(entry, str):
                env[var] = final_names.get(entry, entry)
            elif isinstance(entry, dict) and isinstance(entry.get("secret"), str):
                entry["secret"] = final_names.get(entry["secret"], entry["secret"])
    return renamed


def require_secret_name(name: str) -> None:
    if not SECRET_NAME.match(name):
        raise RequestError(f"{name!r} is not a valid secret name (UPPER_CASE)")


def first_free_name(base: str, unavailable: set[str]) -> str:
    candidate, suffix = base, 2
    while candidate in unavailable:
        candidate, suffix = f"{base}_{suffix}", suffix + 1
    return candidate


def presets_label(presets: list[Preset]) -> str:
    return ("preset " if len(presets) == 1 else "presets ") + ", ".join(preset.name for preset in presets)


class Broker:
    def __init__(self, data_dir: Path, config: Config, vault: Vault, state: StateTable, audit: Audit) -> None:
        self.data_dir = data_dir
        self.config = config
        self.vault = vault
        self.state = state
        self.audit = audit
        self.request_listeners: list[Callable[[Request], None]] = []

    def _announce(self, request: Request) -> None:
        self._log_request(request)
        self._show(request)

    def _log_request(self, request: Request, **extra: Any) -> None:
        self.audit.event(
            "request",
            id=request.id,
            kind=request.kind,
            pid=request.provenance.pid,
            reason=request.reason or "(no reason given)",
            presets=list(request.presets),
            vars=sorted(request.mapping),
            **extra,
        )

    def _announce_or_grant(self, request: Request, presets: list[Preset]) -> None:
        """Put the request on the console, or approve it at once when none of its keys needs approval."""
        if self.needs_approval(request.mapping, presets):
            self._announce(request)
        else:
            self._log_request(request, approval=AUTO)
            self.approve(request, by=AUTO)

    def _show(self, request: Request) -> None:
        for listener in self.request_listeners:
            listener(request)

    def preset_named(self, name: str) -> Preset:
        preset = self.config.presets.get(name)
        if preset is None:
            known = ", ".join(sorted(self.config.presets)) or "(none)"
            raise RequestError(f"unknown preset {name!r}; known presets: {known}")
        return preset

    def mapping_from_presets(self, names: list[str], items: list[str]) -> tuple[dict[str, str], list[Preset]]:
        """The variables of all named presets together, narrowed to items (VAR or VAR=SECRET) when any are given.
        A variable the presets map to different secrets is refused unless an item picks one, since a run can hold only one value."""
        presets = [self.preset_named(name) for name in dict.fromkeys(names)]
        label = presets_label(presets)
        choices: dict[str, dict[str, str]] = {}
        for preset in presets:
            for var, entry in preset.env.items():
                choices.setdefault(var, {}).setdefault(entry.secret, preset.name)
        wanted = {var: secret for var, _, secret in (item.partition("=") for item in items)} or dict.fromkeys(choices, "")
        missing = sorted(set(wanted) - set(choices))
        if missing:
            raise RequestError(f"not in {label}: {', '.join(missing)}")
        mapping: dict[str, str] = {}
        for var, secret in wanted.items():
            options = choices[var]
            if secret and secret not in options:
                raise RequestError(f"{var} maps to {' or '.join(options)} in {label}, not {secret}; drop the =SECRET part or request it without a preset")
            if not secret and len(options) > 1:
                sources = " and ".join(f"{option} in {source}" for option, source in options.items())
                raise RequestError(f"{var} is {sources}; leave one of those presets out, or choose with --with {var}=SECRET plus the other variables you need")
            mapping[var] = secret or next(iter(options))
        return mapping, presets

    def mapping_from_with(self, items: list[str]) -> dict[str, str]:
        if not items:
            raise RequestError("no variables requested: pass a preset or --with VAR[=SECRET],...")
        mapping: dict[str, str] = {}
        for item in items:
            var, _, secret = item.partition("=")
            secret = secret or var
            if not VAR_NAME.match(var):
                raise RequestError(f"{var!r} is not a valid environment variable name")
            require_secret_name(secret)
            if secret not in self.vault:
                raise RequestError(f"secret {secret} is not in the vault; see `envh list`")
            mapping[var] = secret
        return mapping

    def policy_for_var(self, var: str, secret: str, presets: list[Preset]) -> SecretPolicy:
        """When several presets supply the variable, the strictest approval wins."""
        policies = [self.config.effective_policy(preset.env[var]) for preset in presets if var in preset.env and preset.env[var].secret == secret]
        return max(policies or [self.config.policy_for(secret)], key=lambda policy: KEY_APPROVALS.index(policy.approval))

    def needs_approval(self, mapping: dict[str, str], presets: list[Preset]) -> bool:
        return any(self.policy_for_var(var, secret, presets).approval != AUTO for var, secret in mapping.items())

    def split_per_run(self, mapping: dict[str, str], presets: list[Preset]) -> tuple[dict[str, str], dict[str, str]]:
        sessionable: dict[str, str] = {}
        per_run: dict[str, str] = {}
        for var, secret in mapping.items():
            if self.policy_for_var(var, secret, presets).approval == "per-run":
                per_run[var] = secret
            else:
                sessionable[var] = secret
        return sessionable, per_run

    def session_cap(self, mapping: dict[str, str], presets: list[Preset]) -> timedelta:
        cap = MAX_SESSION
        for preset in presets:
            if preset.max_session is not None:
                cap = min(cap, preset.max_session)
        for var, secret in mapping.items():
            cap = min(cap, self.policy_for_var(var, secret, presets).max_session)
        return cap

    def env_for(self, mapping: dict[str, str]) -> dict[str, str]:
        return {var: self.vault.get(secret) for var, secret in mapping.items()}

    def request_session(
        self,
        mapping: dict[str, str],
        presets: list[Preset],
        minutes: int,
        reason: str | None,
        command: tuple[str, ...],
        provenance: Provenance,
    ) -> Request:
        if minutes <= 0:
            raise RequestError("--minutes must be a positive number")
        sessionable, per_run = self.split_per_run(mapping, presets)
        if not sessionable:
            raise RequestError("every requested secret is per-run; run without a session instead")
        requested = timedelta(minutes=minutes)
        granted = min(requested, self.session_cap(sessionable, presets))
        request = self.state.new_request(
            kind="session",
            mapping=sessionable,
            provenance=provenance,
            reason=reason,
            command=command,
            presets=tuple(preset.name for preset in presets),
            requested=requested,
            granted=granted,
            summary={"excluded_per_run": sorted(per_run)},
        )
        self._announce_or_grant(request, presets)
        return request

    def owned_session(self, session_id: str, uid: int) -> Session:
        session = self.state.session(session_id)
        if session.provenance.uid != uid:
            raise RequestError(f"session {session_id[:8]} belongs to another user")
        return session

    def resolve_run_in_session(self, session_id: str, items: list[str], uid: int) -> tuple[Session, dict[str, str]]:
        session = self.owned_session(session_id, uid)
        mapping: dict[str, str] = {} if items else dict(session.mapping)
        for item in items:
            var, _, secret = item.partition("=")
            if var not in session.mapping:
                raise RequestError(f"{var} is not covered by session {session_id[:8]}; start a session that includes it, or run without --session")
            if secret and secret != session.mapping[var]:
                raise RequestError(f"{var} maps to {session.mapping[var]} in session {session_id[:8]}, not {secret}")
            mapping[var] = session.mapping[var]
        presets = [self.config.presets[name] for name in session.presets if name in self.config.presets]
        _, per_run = self.split_per_run(mapping, presets)
        if per_run:
            raise RequestError(f"approval is now per-run for {', '.join(sorted(per_run))}; run without --session")
        if session.auto_granted:
            needing = sorted(var for var, secret in mapping.items() if self.policy_for_var(var, secret, presets).approval != AUTO)
            if needing:
                raise RequestError(f"approval is now needed for {', '.join(needing)}; start a new session, or run without --session")
        return session, mapping

    def request_run(
        self,
        mapping: dict[str, str],
        presets: list[Preset],
        reason: str | None,
        command: tuple[str, ...],
        provenance: Provenance,
    ) -> Request:
        request = self.state.new_request(
            kind="run",
            mapping=mapping,
            provenance=provenance,
            reason=reason,
            command=command,
            presets=tuple(preset.name for preset in presets),
        )
        self._announce_or_grant(request, presets)
        return request

    def validate_preset_yaml(self, text: str) -> dict[str, Any]:
        # Refuse an oversized document before parsing it. YAML parsing (and the validation that
        # follows) runs synchronously on the broker's single event loop, so a large payload would
        # block every other client and the console; presets are small, so this cap is generous.
        if len(text) > MAX_PRESET_BYTES:
            raise RequestError(f"preset document is larger than {MAX_PRESET_BYTES // 1024} KiB; presets are small, so it is refused before parsing")
        _, raw = parse_presets(text, set(self.vault.names()))
        return raw

    def merged_presets(self, additions: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self.config.presets_raw)
        merged.update(additions)
        return merged

    def presets_diff(self, merged: dict[str, Any]) -> str:
        before = dump_presets(self.config.presets_raw).splitlines(keepends=True)
        after = dump_presets(merged).splitlines(keepends=True)
        return "".join(difflib.unified_diff(before, after, fromfile=f"{PRESETS_FILE} (current)", tofile=f"{PRESETS_FILE} (proposed)"))

    def request_preset(self, text: str, reason: str | None, provenance: Provenance) -> Request:
        try:
            additions = self.validate_preset_yaml(text)
        except ConfigError as error:
            raise RequestError(str(error)) from error
        if not additions:
            raise RequestError("the proposal defines no presets")
        merged = self.merged_presets(additions)
        diff = self.presets_diff(merged)
        if not diff:
            raise RequestError("the proposal changes nothing")
        request = self.state.new_request(
            kind="preset",
            mapping={},
            provenance=provenance,
            reason=reason,
            summary={"presets": sorted(additions), "additions": additions, "diff": diff, "merged": merged},
        )
        self._announce(request)
        return request

    def request_import(
        self,
        secrets: dict[str, str],
        presets: dict[str, Any],
        reason: str | None,
        provenance: Provenance,
    ) -> Request:
        if not secrets and not presets:
            raise RequestError("nothing to import")
        for name, value in secrets.items():
            require_secret_name(name)
            if not isinstance(value, str) or not value:
                raise RequestError(f"secret {name} has an empty value")
        # Validate the SUBMITTED presets against the names the client actually provided (plus the
        # vault), NOT against the post-dedup rename targets. import_resolution classifies each value
        # by equality with a stored secret and invents a rename target (NAME_2) for a value that
        # differs; validating the renamed result before approval let a client point a preset at that
        # target and read the equality bit back from whether the reply was an error or pending — an
        # unapproved value oracle on any stored secret. This check is value-independent, so the
        # pre-approval reply no longer depends on whether a submitted value matches a stored one. The
        # authoritative check on the renamed presets still runs at approval time (merged_for_approval).
        try:
            parse_presets(dump_presets(presets), set(self.vault.names()) | set(secrets))
        except ConfigError as error:
            raise RequestError(str(error)) from error
        resolution = self.import_resolution(secrets, presets)
        merged = self.merged_presets(resolution["additions"])
        request = self.state.new_request(
            kind="import",
            mapping={},
            provenance=provenance,
            reason=reason,
            summary={
                **resolution,
                "sent": {"secrets": secrets, "presets": presets},
                "fingerprints": {name: fingerprint(value) for name, value in secrets.items()},
                "presets": sorted(presets),
                "diff": self.presets_diff(merged),
                "merged": merged,
            },
        )
        self._announce(request)
        return request

    def import_resolution(self, secrets: dict[str, str], presets: dict[str, Any]) -> dict[str, Any]:
        """Where each imported secret goes, so that an import never overwrites a stored secret: a value the vault already
        holds is reused under its stored name, and a name the vault holds with a different value gets a number (NAME_2)."""
        stored_by_value: dict[str, str] = {}
        for name in self.vault.names():
            stored_by_value.setdefault(self.vault.get(name), name)
        unavailable = set(self.vault.names()) | set(secrets)
        final_names: dict[str, str] = {}
        for name, value in secrets.items():
            if name in self.vault and self.vault.get(name) == value:
                final_names[name] = name
            elif value in stored_by_value:
                final_names[name] = stored_by_value[value]
            elif name not in self.vault:
                final_names[name] = name
            else:
                final_names[name] = first_free_name(name, unavailable)
                unavailable.add(final_names[name])
        to_store = {final_names[name]: value for name, value in secrets.items() if final_names[name] not in self.vault}
        return {
            "secrets": to_store,
            "added": sorted(to_store),
            "unchanged": sorted(name for name, final in final_names.items() if final == name and name in self.vault),
            "reused": {name: final for name, final in final_names.items() if final != name and final in self.vault},
            "renamed": {name: final for name, final in final_names.items() if final != name and final not in self.vault},
            "additions": renamed_presets(presets, final_names),
        }

    def refresh_import(self, request: Request) -> bool:
        """Recompute where an import's secrets go against the current vault; True when anything changed."""
        sent = request.summary["sent"]
        current = self.import_resolution(sent["secrets"], sent["presets"])
        if all(request.summary[key] == value for key, value in current.items()):
            return False
        request.summary.update(current)
        request.summary["merged"] = self.merged_presets(current["additions"])
        request.summary["diff"] = self.presets_diff(request.summary["merged"])
        return True

    def confirm_import_resolution(self, request: Request) -> None:
        """Refuses an import whose names would now differ from what the approver saw, for example after a console `add`."""
        if self.refresh_import(request):
            self._show(request)
            raise RequestError(f"the vault changed since request #{request.id} was shown; it is shown again with the current names, type the passphrase again to approve that")

    def merged_for_approval(self, request: Request, known_secrets: set[str]) -> dict[str, Any]:
        """The presets to write for this request, recomputed against the current files; refuses when they differ from what the approver saw."""
        merged = self.merged_presets(request.summary["additions"])
        if merged != request.summary["merged"]:
            request.summary["merged"] = merged
            request.summary["diff"] = self.presets_diff(merged)
            self._show(request)
            raise RequestError(f"presets changed since request #{request.id} was shown; it is shown again with the current diff, type the passphrase again to approve that")
        try:
            parse_presets(dump_presets(merged), known_secrets)
        except ConfigError as error:
            raise RequestError(f"request #{request.id} no longer validates: {error}") from error
        return merged

    def approve(self, request: Request, by: str = "console") -> None:
        if request.kind == "session":
            session = self.state.create_session(request, auto_granted=by == AUTO)
            request.result = {"session_id": session.id, "expires_at": session.expires_at.isoformat(timespec="seconds")}
            self.audit.event("session_start", session=session.id, presets=list(session.presets), vars=sorted(session.mapping), expires=request.result["expires_at"], pid=request.provenance.pid)
        elif request.kind == "preset":
            self.write_presets(self.merged_for_approval(request, set(self.vault.names())))
            self.audit.event("presets_updated", presets=request.summary["presets"], by=by)
        elif request.kind == "import":
            self.confirm_import_resolution(request)
            merged = self.merged_for_approval(request, set(self.vault.names()) | set(request.summary["secrets"]))
            for name, value in request.summary["secrets"].items():
                self.vault.set(name, value)
            self.vault.save()
            self.write_presets(merged)
            self.audit.event("import_applied", added=request.summary["added"], reused=request.summary["reused"], renamed=request.summary["renamed"], presets=request.summary["presets"])
        self.state.decide(request, approved=True, by=by)
        self.audit.event("decision", id=request.id, outcome="approved", by=by)

    def deny(self, request: Request, by: str = "console") -> None:
        self.state.decide(request, approved=False, by=by)
        self.audit.event("decision", id=request.id, outcome="denied", by=by)

    def check_rename(self, old: str, new: str) -> tuple[str | None, str | None, Config]:
        """Check that old can be renamed to new, changing nothing. Returns the new config.yaml and presets.yaml texts
        (None for a file that stays as it is) and the config they load as.

        A name that config.yaml still has settings for, or that a live session or waiting request points at, is refused:
        the key would quietly take over those settings, or that session would get its value without an approval."""
        if old not in self.vault:
            raise RequestError(f"{old} is not in the vault")
        require_secret_name(new)
        if new in self.vault:
            raise RequestError(f"{new} is already taken")
        if new in self.config.settings.secrets:
            raise RequestError(f"config.yaml still has settings for {new}; remove them in /settings first")
        if new in self.state.names_in_use():
            raise RequestError(f"a live session or waiting request still uses the name {new}; end or answer it first")
        config_text = self._config_text_as_loaded()
        renamed_config = None
        if old in self.config.settings.secrets:
            entries = {(new if name == old else name): entry for name, entry in self.config.settings.secrets.items()}
            renamed_config = render_config(replace(self.config.settings, secrets=entries))
        presets = renamed_presets(self.config.presets_raw, {old: new})
        presets_text = dump_presets(presets)
        config = config_from_text(renamed_config or config_text, presets_text, (set(self.vault.names()) - {old}) | {new})
        return renamed_config, presets_text if presets != self.config.presets_raw else None, config

    def _config_text_as_loaded(self) -> str:
        """config.yaml as it is on disk, refused when it no longer says what the broker loaded, so a change made there
        since is never overwritten."""
        text = (self.data_dir / CONFIG_FILE).read_text()
        if parse_settings(text) != self.config.settings:
            raise RequestError("config.yaml changed on disk since the console loaded it; run /reload first")
        return text

    def rename_secret(self, old: str, new: str) -> None:
        """Rename a stored secret everywhere envh refers to it: the vault, its settings in config.yaml, presets, live
        sessions and requests.

        The new policy and preset files are written and the config they load as is built before the vault is saved, so a
        failed check or write changes nothing. Only moving the written files into place and the audit line come after."""
        renamed_config, presets_text, config = self.check_rename(old, new)
        staged: list[tuple[Path, Path]] = []
        try:
            for text, path in ((renamed_config, self.data_dir / CONFIG_FILE), (presets_text, self.data_dir / PRESETS_FILE)):
                if text is not None:
                    staged.append((stage_private_file(path, text.encode()), path))
            self.vault.rename(old, new)
        except BaseException:
            for temp_path, _ in staged:
                temp_path.unlink(missing_ok=True)
            raise
        self.config = config
        self.state.rename_secret(old, new)
        self._follow_rename_in_waiting_requests(old, new)
        for temp_path, path in staged:
            move_into_place(temp_path, path)
        self.audit.event("secret_renamed", old=old, new=new)

    def _follow_rename_in_waiting_requests(self, old: str, new: str) -> None:
        """Update the secret names that waiting preset proposals and imports carry in their summaries."""
        for request in self.state.pending():
            if request.kind == "preset":
                request.summary["additions"] = renamed_presets(request.summary["additions"], {old: new})
                request.summary["merged"] = self.merged_presets(request.summary["additions"])
                request.summary["diff"] = self.presets_diff(request.summary["merged"])
            elif request.kind == "import":
                self.refresh_import(request)

    def write_presets(self, merged: dict[str, Any]) -> None:
        write_private_file(self.data_dir / PRESETS_FILE, dump_presets(merged).encode())
        self.reload()

    def save_presets(self, presets: dict[str, Any]) -> None:
        """Replace every preset, after checking the whole file as the broker would load it."""
        parsed, raw = parse_presets(dump_presets(presets), set(self.vault.names()))
        changed = sorted(name for name in set(raw) | set(self.config.presets_raw) if raw.get(name) != self.config.presets_raw.get(name))
        write_private_file(self.data_dir / PRESETS_FILE, dump_presets(raw).encode())
        self.config = replace(self.config, presets=parsed, presets_raw=raw)
        self.audit.event("presets_saved", presets=changed)

    def save_settings(self, settings: Settings) -> None:
        """Rewrite config.yaml from settings, after checking it loads together with the current presets."""
        self._config_text_as_loaded()
        text = render_config(settings)
        config = config_from_text(text, dump_presets(self.config.presets_raw), set(self.vault.names()))
        before = self.config.settings
        changed = [field for field in ("users", "notify", "defaults") if getattr(before, field) != getattr(settings, field)]
        changed += sorted(name for name in set(before.secrets) | set(settings.secrets) if before.secrets.get(name) != settings.secrets.get(name))
        write_private_file(self.data_dir / CONFIG_FILE, text.encode())
        self.config = config
        self.audit.event("settings_saved", changed=changed)

    def store_secret(self, name: str, value: str) -> None:
        """Add a secret, or replace the value of one."""
        require_secret_name(name)
        if not value:
            raise RequestError("an empty value can't be stored")
        replacing = name in self.vault
        self.vault.store(name, value)
        self.audit.event("admin_replace" if replacing else "admin_add", secret=name)
        self.reload()

    def remove_secret(self, name: str) -> None:
        """Remove a secret and its settings. A secret a preset uses is refused, since such a preset would stop the broker
        from starting."""
        if name not in self.vault:
            raise VaultError(f"{name} is not in the vault")
        users = self.config.presets_using(name)
        if users:
            raise RequestError(f"{name} is used by {'preset' if len(users) == 1 else 'presets'} {', '.join(users)}; remove it there first")
        has_settings = name in self.config.settings.secrets
        if has_settings:
            self._config_text_as_loaded()
        self.vault.delete(name)
        self.audit.event("admin_rm", secret=name)
        if has_settings:
            entries = {other: entry for other, entry in self.config.settings.secrets.items() if other != name}
            self.save_settings(replace(self.config.settings, secrets=entries))

    def reveal_secret(self, name: str) -> str:
        """A secret's value, for the console to show at the human's request; each time is audited."""
        value = self.vault.get(name)
        self.audit.event("secret_revealed", secret=name)
        return value

    def change_passphrase(self, new_passphrase: str) -> None:
        if len(new_passphrase) < MIN_PASSPHRASE_LENGTH:
            raise VaultError(f"use at least {MIN_PASSPHRASE_LENGTH} characters")
        self.vault.change_passphrase(new_passphrase)
        self.audit.event("vault_passphrase_changed")

    def reload(self) -> None:
        self.config = load_config(self.data_dir, set(self.vault.names()))
        self.audit.event("reload", secrets=len(self.vault.names()), presets=len(self.config.presets))

    def end_session(self, session_id: str, by: str, uid: int | None = None) -> Session:
        if uid is not None:
            self.owned_session(session_id, uid)
        session = self.state.end_session(session_id)
        self.audit.event("session_end", session=session.id, by=by)
        return session

    def list_payload(self, for_uid: int | None = None) -> dict[str, Any]:
        secrets = []
        for name in self.vault.names():
            policy = self.config.policy_for(name)
            secrets.append({"name": name, "approval": policy.approval, "max_session": format_duration(policy.max_session), "description": self.config.description_for(name)})
        presets = []
        for preset in self.config.presets.values():
            env = []
            for var, entry in preset.env.items():
                env.append({"var": var, "secret": entry.secret, "approval": self.config.effective_policy(entry).approval, "in_vault": entry.secret in self.vault})
            mapping = {var: entry.secret for var, entry in preset.env.items()}
            presets.append({"name": preset.name, "max_session": format_duration(self.session_cap(mapping, [preset])), "env": env})
        sessions = [session for session in self.state.live_sessions() if for_uid is None or session.provenance.uid == for_uid]
        return {"secrets": secrets, "presets": presets, "sessions": [self.session_payload(session) for session in sessions]}

    def session_payload(self, session: Session) -> dict[str, Any]:
        return {
            "id": session.id,
            "presets": list(session.presets),
            "vars": sorted(session.mapping),
            "expires_at": session.expires_at.isoformat(timespec="seconds"),
            "reason": session.reason,
            "pid": session.provenance.pid,
            "uid": session.provenance.uid,
        }

    def status_payload(self, for_uid: int | None = None) -> dict[str, Any]:
        def mine(provenance: Provenance) -> bool:
            return for_uid is None or provenance.uid == for_uid

        return {
            "pending": [
                {"id": request.id, "kind": request.kind, "reason": request.reason, "vars": sorted(request.mapping), "pid": request.provenance.pid, "created_at": request.created_at.isoformat(timespec="seconds")}
                for request in self.state.pending()
                if mine(request.provenance)
            ],
            "runs": [
                {"id": run.id, "vars": sorted(run.mapping), "session": run.session_id, "command": list(run.command), "pid": run.provenance.pid, "started_at": run.started_at.isoformat(timespec="seconds")}
                for run in self.state.active_runs()
                if mine(run.provenance)
            ],
            "sessions": [self.session_payload(session) for session in self.state.live_sessions() if mine(session.provenance)],
        }

