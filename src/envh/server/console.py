"""The trusted console: approval prompts and admin commands on the broker's own terminal."""

from __future__ import annotations

import asyncio
import difflib
import os
import pwd
import re
import shutil
import subprocess
import termios
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from envh.common import fingerprint
from envh.core.broker import Broker, RequestError
from envh.core.config import CONFIG_FILE, PRESETS_FILE, SECRET_NAME, ConfigError, parse_config, parse_presets
from envh.core.durations import format_duration
from envh.core.state import Request, StateError
from envh.core.vault import VaultError, write_private_file

CODE_LINE = re.compile(r"^([n]?)(\d{4})$")
BELL = "\a"
HELP = """commands:
  <code>            approve the request showing that code
  n<code>           deny it
  add SECRET        store a new secret (value typed hidden)
  rm SECRET         remove a secret
  secrets | presets | sessions | runs | pending
  preset rm NAME    remove a preset
  edit config       open config.yaml in an editor; validated before it is saved
  edit presets      same for presets.yaml (optionally: edit presets vim)
  reload            re-read config.yaml and presets.yaml
  help | quit"""


@dataclass(frozen=True)
class ParsedLine:
    kind: str
    code: str = ""
    command: str = ""
    args: tuple[str, ...] = ()


def parse_line(text: str) -> ParsedLine:
    stripped = text.strip()
    if not stripped:
        return ParsedLine(kind="empty")
    match = CODE_LINE.match(stripped)
    if match:
        return ParsedLine(kind="deny" if match.group(1) else "approve", code=match.group(2))
    parts = stripped.split()
    return ParsedLine(kind="command", command=parts[0].lower(), args=tuple(parts[1:]))


def render_request(request: Request, now: datetime) -> list[str]:
    reason = f'"{request.reason}"' if request.reason else "(no reason given)   <-- ask why before approving"
    header = f"{BELL}[{now:%H:%M:%S}] {request.kind.upper()} REQUEST #{request.id}   code {request.code}   pid {request.provenance.pid}  uid {request.provenance.uid}"
    lines = [header, f"   from:     {login_name(request.provenance.uid)}", f"   reason:   {reason}"]
    if request.kind == "session":
        lines.append(f"   preset:   {request.preset or '(ad hoc)'}")
        lines.extend(f"   {var:<28} <- {secret}" for var, secret in sorted(request.mapping.items()))
        excluded = request.summary.get("excluded_per_run") or []
        if excluded:
            lines.append(f"   excluded (per-run secrets, will prompt on every run): {', '.join(excluded)}")
        if request.requested is not None and request.granted is not None:
            capped = "" if request.granted == request.requested else f"  (capped from {format_duration(request.requested)})"
            lines.append(f"   duration: {format_duration(request.granted)}{capped}")
    elif request.kind == "run":
        lines.append(f"   preset:   {request.preset or '(ad hoc)'}")
        lines.extend(f"   {var:<28} <- {secret}   HANDOUT: value visible to the process" for var, secret in sorted(request.mapping.items()))
    elif request.kind == "preset":
        lines.append(f"   presets:  {', '.join(request.summary.get('presets', []))}")
        lines.extend("   " + diff_line for diff_line in request.summary.get("diff", "").rstrip().splitlines())
    elif request.kind == "import":
        fingerprints = request.summary.get("fingerprints", {})
        for label in ("added", "changed", "unchanged"):
            names = request.summary.get(label) or []
            for name in names:
                lines.append(f"   {label:<9} {name:<32} {fingerprints.get(name, '')}")
        presets = request.summary.get("presets") or []
        if presets:
            lines.append(f"   presets:  {', '.join(presets)}")
            lines.extend("   " + diff_line for diff_line in request.summary.get("diff", "").rstrip().splitlines())
    if request.command:
        lines.append(f"   claims:   {' '.join(request.command)}")
    lines.append(f"   provenance: {request.provenance.cmdline}")
    lines.append(f"   type {request.code} to approve, n{request.code} to deny")
    return lines


class Console:
    def __init__(self, broker: Broker, reader: asyncio.StreamReader, say: Callable[[str], None], tty_fd: int | None) -> None:
        self.broker = broker
        self.reader = reader
        self.say = say
        self.tty_fd = tty_fd
        self.quit_requested = asyncio.Event()
        self._busy = False
        self._queued: list[Request] = []
        broker.request_listeners.append(self.on_request)

    def on_request(self, request: Request) -> None:
        if self._busy:
            self._queued.append(request)
        else:
            self.show_request(request)

    def show_request(self, request: Request) -> None:
        for line in render_request(request, self.broker.state.now()):
            self.say(line)

    def _flush_queued(self) -> None:
        queued, self._queued = self._queued, []
        for request in queued:
            if request.pending:
                self.show_request(request)

    async def run(self) -> None:
        self.say("console ready; type help for commands")
        while not self.quit_requested.is_set():
            line = await self.reader.readline()
            if not line:
                self.say("console input closed; shutting down")
                self.quit_requested.set()
                break
            self._busy = True
            try:
                await self.handle_line(line.decode(errors="replace"))
            except (RequestError, StateError, ConfigError, VaultError) as error:
                self.say(f"error: {error}")
            except Exception:
                self.say("unexpected error in the console (the broker keeps running):")
                self.say(traceback.format_exc())
            finally:
                self._busy = False
                self._flush_queued()

    async def handle_line(self, text: str) -> None:
        parsed = parse_line(text)
        if parsed.kind == "empty":
            return
        if parsed.kind in ("approve", "deny"):
            request = self.broker.state.find_by_code(parsed.code)
            if request is None:
                self.say(f"no pending request with code {parsed.code}")
                return
            if parsed.kind == "approve":
                self.broker.approve(request)
                self.say(f"approved #{request.id}")
            else:
                self.broker.deny(request)
                self.say(f"denied #{request.id}")
            return
        await self._command(parsed)

    async def _command(self, parsed: ParsedLine) -> None:
        command, args = parsed.command, parsed.args
        if command == "help":
            self.say(HELP)
        elif command == "quit":
            self.quit_requested.set()
        elif command == "add" and len(args) == 1:
            await self._add(args[0])
        elif command == "rm" and len(args) == 1:
            await self._remove(args[0])
        elif command == "secrets":
            for entry in self.broker.list_payload()["secrets"]:
                self.say(f"   {entry['name']:<32} {entry['approval']:<8} max session {entry['max_session']}")
        elif command == "presets":
            self._show_presets()
        elif command == "sessions":
            for session in self.broker.state.live_sessions():
                self.say(f"   {session.id}  {login_name(session.provenance.uid):<12} {session.preset or '(ad hoc)':<24} expires {session.expires_at:%H:%M:%S}  vars {', '.join(sorted(session.mapping))}  reason {session.reason or '-'}")
        elif command == "runs":
            for run in self.broker.state.active_runs():
                self.say(f"   run #{run.id} pid {run.provenance.pid} vars {', '.join(sorted(run.mapping))}  {' '.join(run.command)}")
        elif command == "pending":
            for request in self.broker.state.pending():
                self.show_request(request)
        elif command == "preset" and len(args) == 2 and args[0] == "rm":
            await self._remove_preset(args[1])
        elif command == "edit" and 1 <= len(args) <= 2 and args[0] in ("config", "presets"):
            await self._edit(args[0], args[1] if len(args) == 2 else None)
        elif command == "reload":
            self.broker.reload()
            self.say("reloaded")
        else:
            self.say(f"unknown command: {parsed.command} {' '.join(args)}".rstrip())
            self.say(HELP)

    def _show_presets(self) -> None:
        for preset in self.broker.list_payload()["presets"]:
            self.say(f"   {preset['name']}  (max session {preset['max_session']})")
            for entry in preset["env"]:
                note = "" if entry["in_vault"] else "   MISSING FROM VAULT"
                per_run = "  per-run" if entry["approval"] == "per-run" else ""
                self.say(f"      {entry['var']:<28} <- {entry['secret']}{per_run}{note}")

    async def _add(self, name: str) -> None:
        if not SECRET_NAME.match(name):
            raise RequestError(f"{name!r} is not a valid secret name (UPPER_CASE)")
        if name in self.broker.vault:
            if not await self.confirm(f"{name} already exists; replace its value?"):
                return
        value = await self.read_hidden(f"value for {name} (hidden): ")
        if not value:
            self.say("empty value; nothing stored")
            return
        if not await self.confirm(f"store {name} = {fingerprint(value)}?"):
            return
        self.broker.vault.set(name, value)
        self.broker.vault.save()
        self.broker.reload()
        self.broker.audit.event("admin_add", secret=name)

    async def _remove(self, name: str) -> None:
        if name not in self.broker.vault:
            raise VaultError(f"secret {name} is not in the vault")
        users = [preset.name for preset in self.broker.config.presets.values() if any(entry.secret == name for entry in preset.env.values())]
        if users:
            self.say(f"   used by presets: {', '.join(users)} (they will fail validation until updated)")
        if not await self.confirm(f"remove {name}?"):
            return
        self.broker.vault.remove(name)
        self.broker.vault.save()
        self.broker.audit.event("admin_rm", secret=name)
        try:
            self.broker.reload()
        except ConfigError as error:
            self.say(f"warning: presets no longer validate: {error}")

    async def _remove_preset(self, name: str) -> None:
        if name not in self.broker.config.presets_raw:
            raise RequestError(f"unknown preset {name!r}")
        if not await self.confirm(f"remove preset {name}?"):
            return
        merged = dict(self.broker.config.presets_raw)
        del merged[name]
        self.broker.write_presets(merged)
        self.broker.audit.event("preset_removed", preset=name)

    async def _edit(self, which: str, editor: str | None) -> None:
        path = self.broker.data_dir / (CONFIG_FILE if which == "config" else PRESETS_FILE)
        editor = editor or pick_editor()
        if editor is None:
            raise RequestError("no editor found; install nano or pass one: edit config vim")
        if shutil.which(editor) is None:
            raise RequestError(f"editor {editor!r} not found")
        original = path.read_text()
        scratch = path.with_name(path.name + ".edit")
        write_private_file(scratch, original.encode())
        try:
            while True:
                self.say(f"opening {path} with {editor}; save and exit to validate")
                subprocess.run([editor, str(scratch)], check=False)
                new_text = scratch.read_text()
                if new_text == original:
                    self.say("no changes")
                    return
                try:
                    if which == "config":
                        parse_config(new_text)
                    else:
                        parse_presets(new_text, set(self.broker.vault.names()))
                except ConfigError as error:
                    self.say(f"rejected: {error}")
                    if await self.confirm("edit again?"):
                        continue
                    self.say("discarded; the file is unchanged")
                    return
                diff = difflib.unified_diff(original.splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile=f"{path.name} (current)", tofile=f"{path.name} (edited)")
                for line in "".join(diff).rstrip().splitlines():
                    self.say("   " + line)
                if not await self.confirm("save this?"):
                    self.say("discarded; the file is unchanged")
                    return
                write_private_file(path, new_text.encode())
                self.broker.reload()
                self.broker.audit.event("config_edited", file=path.name, editor=editor)
                return
        finally:
            scratch.unlink(missing_ok=True)

    async def confirm(self, prompt: str) -> bool:
        self.say(f"{prompt} [y/N] ")
        line = await self.reader.readline()
        return line.decode(errors="replace").strip().lower() in ("y", "yes")

    async def read_hidden(self, prompt: str) -> str:
        self.say(prompt)
        if self.tty_fd is None:
            line = await self.reader.readline()
            return line.decode(errors="replace").rstrip("\n")
        attributes = termios.tcgetattr(self.tty_fd)
        quiet = list(attributes)
        quiet[3] = quiet[3] & ~termios.ECHO
        termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, quiet)
        try:
            line = await self.reader.readline()
        finally:
            termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, attributes)
            self.say("")
        return line.decode(errors="replace").rstrip("\n")


def login_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def pick_editor() -> str | None:
    for candidate in (os.environ.get("VISUAL"), os.environ.get("EDITOR"), "nano", "vi"):
        if candidate and shutil.which(candidate):
            return candidate
    return None
