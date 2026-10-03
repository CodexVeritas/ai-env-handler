"""The broker: policy decisions, request lifecycle, and the side effects of approvals."""

from __future__ import annotations

import difflib
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from envh.common import fingerprint
from envh.server.audit import Audit
from envh.server.config import (
    PRESETS_FILE,
    SECRET_NAME,
    VAR_NAME,
    Config,
    ConfigError,
    Preset,
    dump_presets,
    load_config,
    parse_presets,
)
from envh.server.durations import MAX_SESSION, format_duration
from envh.server.state import Provenance, Request, Session, StateTable
from envh.server.vault import Vault, write_private_file


class RequestError(ValueError):
    """A request that cannot be granted as asked; the message is shown to the requester."""


class Broker:
    def __init__(self, data_dir: Path, config: Config, vault: Vault, state: StateTable, audit: Audit) -> None:
        self.data_dir = data_dir
        self.config = config
        self.vault = vault
        self.state = state
        self.audit = audit
        self.request_listeners: list[Callable[[Request], None]] = []

    def _announce(self, request: Request) -> None:
        self.audit.event(
            "request",
            id=request.id,
            kind=request.kind,
            pid=request.provenance.pid,
            reason=request.reason or "(no reason given)",
            preset=request.preset,
            vars=sorted(request.mapping),
        )
        for listener in self.request_listeners:
            listener(request)

    def mapping_from_preset(self, name: str) -> tuple[dict[str, str], Preset]:
        preset = self.config.presets.get(name)
        if preset is None:
            known = ", ".join(sorted(self.config.presets)) or "(none)"
            raise RequestError(f"unknown preset {name!r}; known presets: {known}")
        return {var: entry.secret for var, entry in preset.env.items()}, preset

    def mapping_from_with(self, items: list[str]) -> dict[str, str]:
        if not items:
            raise RequestError("no variables requested: pass a preset or --with VAR[=SECRET],...")
        mapping: dict[str, str] = {}
        for item in items:
            var, _, secret = item.partition("=")
            secret = secret or var
            if not VAR_NAME.match(var):
                raise RequestError(f"{var!r} is not a valid environment variable name")
            if not SECRET_NAME.match(secret):
                raise RequestError(f"{secret!r} is not a valid secret name (UPPER_CASE)")
            if secret not in self.vault:
                raise RequestError(f"secret {secret} is not in the vault; see `envh list`")
            mapping[var] = secret
        return mapping

    def policy_for_var(self, var: str, secret: str, preset: Preset | None):
        if preset is not None and var in preset.env and preset.env[var].secret == secret:
            return self.config.effective_policy(preset.env[var])
        return self.config.policy_for(secret)

    def split_per_run(self, mapping: dict[str, str], preset: Preset | None) -> tuple[dict[str, str], dict[str, str]]:
        sessionable: dict[str, str] = {}
        per_run: dict[str, str] = {}
        for var, secret in mapping.items():
            if self.policy_for_var(var, secret, preset).approval == "per-run":
                per_run[var] = secret
            else:
                sessionable[var] = secret
        return sessionable, per_run

    def session_cap(self, mapping: dict[str, str], preset: Preset | None) -> timedelta:
        cap = MAX_SESSION
        if preset is not None and preset.max_session is not None:
            cap = min(cap, preset.max_session)
        for var, secret in mapping.items():
            cap = min(cap, self.policy_for_var(var, secret, preset).max_session)
        return cap

    def env_for(self, mapping: dict[str, str]) -> dict[str, str]:
        return {var: self.vault.get(secret) for var, secret in mapping.items()}

    def request_session(
        self,
        mapping: dict[str, str],
        preset: Preset | None,
        minutes: int,
        reason: str | None,
        command: tuple[str, ...],
        provenance: Provenance,
    ) -> Request:
        if minutes <= 0:
            raise RequestError("--minutes must be a positive number")
        sessionable, per_run = self.split_per_run(mapping, preset)
        if not sessionable:
            raise RequestError("every requested secret is per-run; run without a session instead")
        requested = timedelta(minutes=minutes)
        granted = min(requested, self.session_cap(sessionable, preset))
        request = self.state.new_request(
            kind="session",
            mapping=sessionable,
            provenance=provenance,
            reason=reason,
            command=command,
            preset=preset.name if preset else None,
            requested=requested,
            granted=granted,
            summary={"excluded_per_run": sorted(per_run)},
        )
        self._announce(request)
        return request

    def resolve_run_in_session(self, session_id: str, items: list[str]) -> tuple[Session, dict[str, str]]:
        session = self.state.session(session_id)
        if not items:
            return session, dict(session.mapping)
        mapping: dict[str, str] = {}
        for item in items:
            var, _, secret = item.partition("=")
            if var not in session.mapping:
                raise RequestError(f"{var} is not covered by session {session_id[:8]}; start a session that includes it, or run without --session")
            if secret and secret != session.mapping[var]:
                raise RequestError(f"{var} maps to {session.mapping[var]} in session {session_id[:8]}, not {secret}")
            mapping[var] = session.mapping[var]
        return session, mapping

    def request_run(
        self,
        mapping: dict[str, str],
        preset: Preset | None,
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
            preset=preset.name if preset else None,
        )
        self._announce(request)
        return request

    def validate_preset_yaml(self, text: str) -> dict[str, Any]:
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
            summary={"presets": sorted(additions), "diff": diff, "merged": merged},
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
            if not SECRET_NAME.match(name):
                raise RequestError(f"{name!r} is not a valid secret name (UPPER_CASE)")
            if not isinstance(value, str) or not value:
                raise RequestError(f"secret {name} has an empty value")
        known_after = set(self.vault.names()) | set(secrets)
        try:
            parse_presets(dump_presets(presets), known_after)
        except ConfigError as error:
            raise RequestError(str(error)) from error
        added = sorted(name for name in secrets if name not in self.vault)
        changed = sorted(name for name in secrets if name in self.vault and self.vault.get(name) != secrets[name])
        unchanged = sorted(name for name in secrets if name in self.vault and self.vault.get(name) == secrets[name])
        merged = self.merged_presets(presets)
        request = self.state.new_request(
            kind="import",
            mapping={},
            provenance=provenance,
            reason=reason,
            summary={
                "added": added,
                "changed": changed,
                "unchanged": unchanged,
                "fingerprints": {name: fingerprint(value) for name, value in secrets.items()},
                "secrets": secrets,
                "presets": sorted(presets),
                "diff": self.presets_diff(merged),
                "merged": merged,
            },
        )
        self._announce(request)
        return request

    def approve(self, request: Request, by: str = "console") -> None:
        if request.kind == "session":
            session = self.state.create_session(request)
            request.result = {"session_id": session.id, "expires_at": session.expires_at.isoformat(timespec="seconds")}
            self.audit.event("session_start", session=session.id, preset=session.preset, vars=sorted(session.mapping), expires=request.result["expires_at"], pid=request.provenance.pid)
        elif request.kind == "preset":
            self.write_presets(request.summary["merged"])
            self.audit.event("presets_updated", presets=request.summary["presets"], by=by)
        elif request.kind == "import":
            for name, value in request.summary["secrets"].items():
                self.vault.set(name, value)
            self.vault.save()
            self.write_presets(request.summary["merged"])
            self.audit.event("import_applied", added=request.summary["added"], changed=request.summary["changed"], presets=request.summary["presets"])
        self.state.decide(request, approved=True, by=by)
        self.audit.event("decision", id=request.id, outcome="approved", by=by)

    def deny(self, request: Request, by: str = "console") -> None:
        self.state.decide(request, approved=False, by=by)
        self.audit.event("decision", id=request.id, outcome="denied", by=by)

    def write_presets(self, merged: dict[str, Any]) -> None:
        write_private_file(self.data_dir / PRESETS_FILE, dump_presets(merged).encode())
        self.reload()

    def reload(self) -> None:
        self.config = load_config(self.data_dir, set(self.vault.names()))
        self.audit.event("reload", secrets=len(self.vault.names()), presets=len(self.config.presets))

    def end_session(self, session_id: str, by: str) -> Session:
        session = self.state.end_session(session_id)
        self.audit.event("session_end", session=session.id, by=by)
        return session

    def list_payload(self) -> dict[str, Any]:
        secrets = []
        for name in self.vault.names():
            policy = self.config.policy_for(name)
            secrets.append({"name": name, "approval": policy.approval, "max_session": format_duration(policy.max_session)})
        presets = []
        for preset in self.config.presets.values():
            env = []
            for var, entry in preset.env.items():
                env.append({"var": var, "secret": entry.secret, "approval": self.config.effective_policy(entry).approval, "in_vault": entry.secret in self.vault})
            mapping = {var: entry.secret for var, entry in preset.env.items()}
            presets.append({"name": preset.name, "max_session": format_duration(self.session_cap(mapping, preset)), "env": env})
        return {"secrets": secrets, "presets": presets, "sessions": [self.session_payload(session) for session in self.state.live_sessions()]}

    def session_payload(self, session: Session) -> dict[str, Any]:
        return {
            "id": session.id,
            "preset": session.preset,
            "vars": sorted(session.mapping),
            "expires_at": session.expires_at.isoformat(timespec="seconds"),
            "reason": session.reason,
            "pid": session.provenance.pid,
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "pending": [
                {"id": request.id, "kind": request.kind, "reason": request.reason, "vars": sorted(request.mapping), "pid": request.provenance.pid, "created_at": request.created_at.isoformat(timespec="seconds")}
                for request in self.state.pending()
            ],
            "runs": [
                {"id": run.id, "vars": sorted(run.mapping), "session": run.session_id, "command": list(run.command), "pid": run.provenance.pid, "started_at": run.started_at.isoformat(timespec="seconds")}
                for run in self.state.active_runs()
            ],
            "sessions": [self.session_payload(session) for session in self.state.live_sessions()],
        }

