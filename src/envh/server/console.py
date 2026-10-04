"""The trusted console: approval prompts and admin commands on the broker's own terminal."""

from __future__ import annotations

import asyncio
import codecs
import difflib
import os
import pwd
import shutil
import subprocess
import termios
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from envh.common import fingerprint, printable
from envh.core.broker import Broker, RequestError
from envh.core.config import CONFIG_FILE, PRESETS_FILE, SECRET_NAME, ConfigError, parse_config_for_broker, parse_presets
from envh.core.durations import format_duration
from envh.core.state import Request, StateError
from envh.core.vault import MIN_PASSPHRASE_LENGTH, VaultError, write_private_file
from envh.server.keys_view import ENTER_FULL_SCREEN, LEAVE_FULL_SCREEN, CheckPassphrase, KeyList, KeyRow, Leave, Rename, render_keys, split_keys

BELL = "\a"
WRONG_PASSPHRASE_PAUSE_SECONDS = 2
KEYS_REFRESH_SECONDS = 0.5
HELP = """A request shows up on its own; type the vault passphrase to approve it, or n to deny it.
commands:
  keys              see your keys; arrows move, F2 renames
  add SECRET        store a new secret (value typed hidden)
  rm SECRET         remove a secret
  secrets | presets | sessions | runs
  preset rm NAME    remove a preset
  edit config       open config.yaml in an editor; validated before it is saved
  edit presets      same for presets.yaml (optionally: edit presets vim)
  passphrase        change the vault passphrase
  reload            re-read config.yaml and presets.yaml
  help | quit
add, rm, preset rm, edit, passphrase and the first rename in keys ask for the vault passphrase too (typed hidden)"""


@dataclass(frozen=True)
class ParsedLine:
    kind: str
    command: str = ""
    args: tuple[str, ...] = ()


def parse_line(text: str) -> ParsedLine:
    parts = text.strip().split()
    if not parts:
        return ParsedLine(kind="empty")
    return ParsedLine(kind="command", command=parts[0].lower(), args=tuple(parts[1:]))


def approval_prompt(request: Request, phrase: str) -> str:
    """The console phrase is shown with every prompt, so a passphrase prompt without it is recognizably not envh."""
    return f"   [{phrase}] vault passphrase to approve #{request.id} (hidden), or n to deny:"


def presets_line(names: tuple[str, ...]) -> str:
    label = "presets:" if len(names) > 1 else "preset:"
    return f"   {label:<10}{', '.join(names) or '(ad hoc)'}"


def render_request(request: Request, now: datetime) -> list[str]:
    reason = f'"{printable(request.reason)}"' if request.reason else "(no reason given)   <-- ask why before approving"
    header = f"[{now:%H:%M:%S}] {request.kind.upper()} REQUEST #{request.id}   pid {request.provenance.pid}  uid {request.provenance.uid}"
    lines = [header, f"   from:     {login_name(request.provenance.uid)}", f"   reason:   {reason}"]
    if request.kind == "session":
        lines.append(presets_line(request.presets))
        lines.extend(f"   {var:<28} <- {secret}" for var, secret in sorted(request.mapping.items()))
        excluded = request.summary.get("excluded_per_run") or []
        if excluded:
            lines.append(f"   excluded (per-run secrets, will prompt on every run): {', '.join(excluded)}")
        if request.requested is not None and request.granted is not None:
            capped = "" if request.granted == request.requested else f"  (capped from {format_duration(request.requested)})"
            lines.append(f"   duration: {format_duration(request.granted)}{capped}")
    elif request.kind == "run":
        lines.append(presets_line(request.presets))
        lines.extend(f"   {var:<28} <- {secret}   HANDOUT: value visible to the process" for var, secret in sorted(request.mapping.items()))
    elif request.kind == "preset":
        lines.append(f"   presets:  {', '.join(request.summary.get('presets', []))}")
        lines.extend("   " + printable(diff_line) for diff_line in request.summary.get("diff", "").rstrip().splitlines())
    elif request.kind == "import":
        reused = request.summary.get("reused") or {}
        renamed = request.summary.get("renamed") or {}
        for name, fingerprint_text in sorted(request.summary.get("fingerprints", {}).items()):
            shown = printable(fingerprint_text)
            if name in reused:
                lines.append(f"   reused    {name:<32} {shown}  same value already stored as {reused[name]}")
            elif name in renamed:
                lines.append(f"   renamed   {name:<32} {shown}  stored as {renamed[name]}: the vault's {name} holds a different value")
            elif name in (request.summary.get("unchanged") or []):
                lines.append(f"   unchanged {name:<32} {shown}")
            else:
                lines.append(f"   added     {name:<32} {shown}")
        presets = request.summary.get("presets") or []
        if presets:
            lines.append(f"   presets:  {', '.join(presets)}")
            lines.extend("   " + printable(diff_line) for diff_line in request.summary.get("diff", "").rstrip().splitlines())
    if request.command:
        lines.append(f"   claims:   {printable(' '.join(request.command))}")
    lines.append(f"   provenance: {printable(request.provenance.cmdline)}")
    return lines


class ConsoleOutput:
    """The console's terminal output. While a full-screen view is open, lines are held and printed when it closes, so
    audit events neither break the view nor get lost."""

    def __init__(self, say: Callable[[str], None], write: Callable[[str], None]) -> None:
        self._say = say
        self._write = write
        self._held: list[str] | None = None

    def say(self, text: str) -> None:
        if self._held is None:
            self._say(text)
        else:
            self._held.append(text)

    def draw(self, frame: str) -> None:
        self._write(frame)

    @contextmanager
    def holding(self) -> Iterator[None]:
        self._held = []
        try:
            yield
        finally:
            held, self._held = self._held, None
            for text in held:
                self._say(text)


class Console:
    """Shows one request at a time and reads the answer with echo off, so a typed passphrase never appears on screen.

    Input typed while a request is shown is only ever an answer to that request. If the request is withdrawn while the
    human may be typing, the next line is discarded unread instead of being taken as a command or as an answer to the
    next request.
    """

    def __init__(self, broker: Broker, reader: asyncio.StreamReader, output: ConsoleOutput, tty_fd: int | None, phrase: str) -> None:
        self.broker = broker
        self.phrase = phrase
        self.reader = reader
        self.output = output
        self.say = output.say
        self.tty_fd = tty_fd
        self.quit_requested = asyncio.Event()
        self.current: Request | None = None
        self._discard_next_line = False
        self._busy = False
        self._queued: list[Request] = []
        broker.request_listeners.append(self.on_request)

    def on_request(self, request: Request) -> None:
        if request is self.current:
            self.show_request(request)
        elif self._busy or self.current is not None or self._discard_next_line:
            self._queued.append(request)
        else:
            self._present(request)

    def show_request(self, request: Request) -> None:
        lines = render_request(request, self.broker.state.now())
        if self.broker.config.notify:
            lines[0] = BELL + lines[0]
        for line in lines:
            self.say(line)
        self.say(approval_prompt(request, self.phrase))

    def _present(self, request: Request) -> None:
        self.current = request
        request.decision.add_done_callback(lambda decision: self._on_decided(request, decision))
        self.show_request(request)
        self.set_echo(False)

    def _on_decided(self, request: Request, decision: asyncio.Future) -> None:
        if request is not self.current or decision.cancelled() or decision.result().outcome != "withdrawn":
            return
        self.current = None
        self._discard_next_line = True
        self.say(f"#{request.id} was withdrawn: the program that asked went away. Press Enter to go on.")

    def _advance(self) -> None:
        if self._busy or self.current is not None or self._discard_next_line:
            return
        while self._queued:
            request = self._queued.pop(0)
            if request.pending:
                self._present(request)
                return
        self.set_echo(True)

    def set_echo(self, enabled: bool) -> None:
        if self.tty_fd is None:
            return
        attributes = termios.tcgetattr(self.tty_fd)
        attributes[3] = attributes[3] | termios.ECHO if enabled else attributes[3] & ~termios.ECHO
        termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, attributes)

    async def run(self) -> None:
        self.say("console ready; requests appear here on their own. Type keys to see your keys, help for commands")
        try:
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
                    self._advance()
        finally:
            self.set_echo(True)

    async def handle_line(self, text: str) -> None:
        if self._discard_next_line:
            self._discard_next_line = False
            self.say("input discarded")
            return
        if self.current is not None:
            await self._answer(self.current, text.rstrip("\n"))
            return
        parsed = parse_line(text)
        if parsed.kind == "command":
            await self._command(parsed)

    async def _answer(self, request: Request, entered: str) -> None:
        if entered.strip().lower() in ("n", "no"):
            self.broker.deny(request)
            self.current = None
            self.say(f"denied #{request.id}")
        elif not entered.strip():
            self.say(approval_prompt(request, self.phrase))
        elif self.broker.vault.matches_passphrase(entered):
            self.broker.approve(request)
            self.current = None
            self.say(f"approved #{request.id}")
        else:
            await self._wrong_passphrase(f"approve #{request.id}")
            if request is self.current:
                self.say(f"#{request.id} is still waiting")
                self.say(approval_prompt(request, self.phrase))

    async def _command(self, parsed: ParsedLine) -> None:
        command, args = parsed.command, parsed.args
        if command == "help":
            self.say(HELP)
        elif command == "keys" and not args:
            await self._keys()
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
                self.say(f"   {session.id}  {login_name(session.provenance.uid):<12} {', '.join(session.presets) or '(ad hoc)':<24} expires {session.expires_at:%H:%M:%S}  vars {', '.join(sorted(session.mapping))}  reason {printable(session.reason or '-')}")
        elif command == "runs":
            for run in self.broker.state.active_runs():
                self.say(f"   run #{run.id} pid {run.provenance.pid} vars {', '.join(sorted(run.mapping))}  {printable(' '.join(run.command))}")
        elif command == "preset" and len(args) == 2 and args[0] == "rm":
            await self._remove_preset(args[1])
        elif command == "edit" and 1 <= len(args) <= 2 and args[0] in ("config", "presets"):
            await self._edit(args[0], args[1] if len(args) == 2 else None)
        elif command == "passphrase" and not args:
            await self._change_passphrase()
        elif command == "reload":
            self.broker.reload()
            self.say("reloaded")
        else:
            self.say(f"unknown command: {parsed.command} {' '.join(args)}".rstrip())
            self.say(HELP)

    async def _keys(self) -> None:
        view = KeyList(self._key_rows())
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        shown = {"frame": "", "waiting": 0}

        def draw() -> None:
            waiting = sum(1 for request in self._queued if request.pending)
            frame = render_keys(view, self.phrase, waiting, self._screen_size())
            if frame != shown["frame"]:
                self.output.draw(frame + (BELL if waiting > shown["waiting"] and self.broker.config.notify else ""))
            shown.update(frame=frame, waiting=waiting)

        with self.output.holding(), self._full_screen():
            while True:
                draw()
                try:
                    data = await asyncio.wait_for(self.reader.read(4096), KEYS_REFRESH_SECONDS)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    return
                for key in split_keys(decoder.decode(data)):
                    action = view.handle(key)
                    if isinstance(action, CheckPassphrase):
                        if self.broker.vault.matches_passphrase(action.typed):
                            action = view.passphrase_accepted()
                        else:
                            view.passphrase_rejected()
                            draw()
                            await self._wrong_passphrase("rename")
                    if isinstance(action, Rename):
                        view.tell("Saving…", "info")
                        draw()
                        self._rename(view, action)
                    elif isinstance(action, Leave):
                        return

    def _rename(self, view: KeyList, rename: Rename) -> None:
        try:
            self.broker.rename_secret(rename.old, rename.new)
        except (RequestError, ConfigError, VaultError, OSError) as error:
            view.tell(f"Nothing renamed: {error}")
            return
        view.renamed(self._key_rows(), rename.new)

    def _key_rows(self) -> list[KeyRow]:
        config = self.broker.config
        return [
            KeyRow(
                name=name,
                used_by=tuple(sorted(preset.name for preset in config.presets.values() if any(entry.secret == name for entry in preset.env.values()))),
                per_run=config.policy_for(name).approval == "per-run",
            )
            for name in self.broker.vault.names()
        ]

    def _screen_size(self) -> os.terminal_size:
        if self.tty_fd is not None:
            try:
                return os.get_terminal_size(self.tty_fd)
            except OSError:
                pass
        return os.terminal_size((80, 24))

    @contextmanager
    def _full_screen(self) -> Iterator[None]:
        """Keys arrive as they are pressed, on the alternate screen, so the console's scrollback comes back unchanged.
        Ctrl-C reaches the view as a key that leaves it, instead of stopping the broker."""
        saved = termios.tcgetattr(self.tty_fd) if self.tty_fd is not None else None
        if saved is not None:
            raw = list(saved)
            raw[3] = raw[3] & ~(termios.ECHO | termios.ICANON | termios.ISIG)
            raw[6] = list(saved[6])
            raw[6][termios.VMIN] = 1
            raw[6][termios.VTIME] = 0
            termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, raw)
        self.output.draw(ENTER_FULL_SCREEN)
        try:
            yield
        finally:
            self.output.draw(LEAVE_FULL_SCREEN)
            if saved is not None:
                termios.tcsetattr(self.tty_fd, termios.TCSADRAIN, saved)

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
        if not await self.check_passphrase(f"add {name}"):
            return
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
        self.broker.audit.event("admin_add", secret=name)
        self.broker.reload()

    async def _remove(self, name: str) -> None:
        if name not in self.broker.vault:
            raise VaultError(f"secret {name} is not in the vault")
        users = [preset.name for preset in self.broker.config.presets.values() if any(entry.secret == name for entry in preset.env.values())]
        if users:
            raise RequestError(f"{name} is used by presets {', '.join(users)}; change or remove them first (edit presets, preset rm NAME), since presets naming a missing secret stop the broker from starting")
        if not await self.check_passphrase(f"rm {name}"):
            return
        if not await self.confirm(f"remove {name}?"):
            return
        self.broker.vault.remove(name)
        self.broker.vault.save()
        self.broker.audit.event("admin_rm", secret=name)
        self.broker.reload()

    async def _remove_preset(self, name: str) -> None:
        if name not in self.broker.config.presets_raw:
            raise RequestError(f"unknown preset {name!r}")
        if not await self.check_passphrase(f"preset rm {name}"):
            return
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
        if not await self.check_passphrase(f"edit {which}"):
            return
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
                        parse_config_for_broker(new_text)
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
                self.broker.audit.event("config_edited", file=path.name, editor=editor)
                self.broker.reload()
                return
        finally:
            scratch.unlink(missing_ok=True)

    async def check_passphrase(self, action: str) -> bool:
        entered = await self.read_hidden(f"vault passphrase for {action} (hidden): ")
        if self.broker.vault.matches_passphrase(entered):
            return True
        await self._wrong_passphrase(action)
        return False

    async def _wrong_passphrase(self, action: str) -> None:
        """A wrong passphrase is audited and pauses the console, so guessing by typing blind is slow and visible."""
        self.broker.audit.event("wrong_passphrase", action=action)
        self.say(f"wrong passphrase; {action} not done")
        await asyncio.sleep(WRONG_PASSPHRASE_PAUSE_SECONDS)

    async def _change_passphrase(self) -> None:
        if not await self.check_passphrase("passphrase change"):
            return
        new_passphrase = await self.read_hidden("new vault passphrase (hidden): ")
        if len(new_passphrase) < MIN_PASSPHRASE_LENGTH:
            self.say(f"use at least {MIN_PASSPHRASE_LENGTH} characters; the passphrase is unchanged")
            return
        if await self.read_hidden("repeat it (hidden): ") != new_passphrase:
            self.say("they do not match; the passphrase is unchanged")
            return
        self.broker.vault.change_passphrase(new_passphrase)
        self.broker.audit.event("vault_passphrase_changed")
        self.say("vault passphrase changed; the vault is re-encrypted with it")

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
