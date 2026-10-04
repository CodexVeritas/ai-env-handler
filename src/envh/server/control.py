"""Control plane: newline-delimited JSON over a Unix socket, one operation per connection."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path
from typing import Any

from envh.platform import peer_credentials
from envh.core.broker import Broker, RequestError
from envh.core.config import ConfigError, Preset
from envh.core.state import Provenance, Request, StateError
from envh.core.vault import VaultError

MAX_LINE = 1024 * 1024
MAX_SOCKET_PATH = 100
HandledErrors = (RequestError, StateError, ConfigError, VaultError)


class ControlServer:
    def __init__(self, broker: Broker, socket_path: Path, socket_mode: int = 0o666) -> None:
        self.broker = broker
        self.socket_path = socket_path
        self.socket_mode = socket_mode
        self._server: asyncio.AbstractServer | None = None
        self._open_writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        if len(str(self.socket_path).encode()) > MAX_SOCKET_PATH:
            raise OSError(f"socket path {self.socket_path} is longer than {MAX_SOCKET_PATH} bytes; Unix sockets need short paths")
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path), limit=MAX_LINE)
        os.chmod(self.socket_path, self.socket_mode)

    async def close(self) -> None:
        """Stop listening and hang up on every client, since wait_closed() waits for connections that only end when the client leaves."""
        if self._server is not None:
            self._server.close()
            for writer in list(self._open_writers):
                writer.close()
            await self._server.wait_closed()
        self.socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._open_writers.add(writer)
        connection = Connection(self.broker, reader, writer)
        try:
            await connection.serve()
        except HandledErrors as error:
            await connection.send({"ok": False, "error": str(error)})
        except (ConnectionError, asyncio.IncompleteReadError) as error:
            self.broker.audit.event("client_connection_lost", pid=connection.provenance.pid, detail=str(error))
        except Exception:
            self.broker.audit.event("error", where="control", detail=traceback.format_exc(limit=5))
            await connection.send({"ok": False, "error": "internal error in the broker; see its console"})
        finally:
            self._open_writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


class Connection:
    def __init__(self, broker: Broker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.broker = broker
        self.reader = reader
        self.writer = writer
        self.provenance = self._provenance()

    def _provenance(self) -> Provenance:
        sock = self.writer.get_extra_info("socket")
        credentials = peer_credentials(sock)
        return Provenance(pid=credentials.pid, uid=credentials.uid, cmdline=credentials.cmdline())

    async def send(self, payload: dict[str, Any]) -> None:
        if self.writer.is_closing():
            return
        self.writer.write(json.dumps(payload, default=str).encode() + b"\n")
        try:
            await self.writer.drain()
        except ConnectionError:
            pass

    async def serve(self) -> None:
        if self.provenance.uid not in self.broker.config.allowed_uids:
            self.broker.audit.event("rejected_uid", uid=self.provenance.uid, pid=self.provenance.pid, cmdline=self.provenance.cmdline)
            raise RequestError(f"uid {self.provenance.uid} is not in the broker's users list; a listed user can add it on the console with `edit config`")
        line = await self.reader.readline()
        if not line:
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RequestError(f"malformed request: {error}") from error
        if not isinstance(message, dict) or not isinstance(message.get("op"), str):
            raise RequestError("malformed request: expected an object with an 'op' field")
        handler = getattr(self, f"op_{message['op']}", None)
        if handler is None:
            raise RequestError(f"unknown operation {message['op']!r}")
        await handler(message)

    def _mapping(self, message: dict[str, Any]) -> tuple[dict[str, str], list[Preset]]:
        names = message.get("presets") or []
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise RequestError("presets must be a list of preset names")
        items = [str(item) for item in message.get("with") or []]
        if names:
            return self.broker.mapping_from_presets(names, items)
        return self.broker.mapping_from_with(items), []

    @staticmethod
    def _command(message: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(part) for part in message.get("command") or ())

    @staticmethod
    def _reason(message: dict[str, Any]) -> str | None:
        reason = message.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise RequestError(f"reason must be a string, got {type(reason).__name__}")
        return reason

    async def _decided_or_withdrawn(self, request: Request) -> bool:
        """Wait for the decision; if the client goes away first, withdraw the request and return False."""
        if await self._wait_decision(request):
            return True
        self.broker.state.withdraw(request)
        self.broker.audit.event("withdrawn", id=request.id)
        return False

    async def _wait_decision(self, request: Request) -> bool:
        """Wait until the request is decided or the client goes away. Returns True if decided."""
        decision = asyncio.ensure_future(asyncio.shield(request.decision))
        while True:
            incoming = asyncio.ensure_future(self.reader.readline())
            done, _ = await asyncio.wait({decision, incoming}, return_when=asyncio.FIRST_COMPLETED)
            if decision in done:
                incoming.cancel()
                await asyncio.gather(incoming, return_exceptions=True)
                return True
            try:
                line = incoming.result()
            except (ConnectionError, ValueError):
                line = b""
            if line == b"":
                decision.cancel()
                return False

    def _pending(self, request: Request, **extra: Any) -> dict[str, Any]:
        """The first reply to a request that waits for the console; notify tells the client to alert the human."""
        return {"ok": True, "request_id": request.id, "pending": True, "notify": self.broker.config.notify, **extra}

    async def _reply_decision(self, request: Request) -> None:
        decision = request.decision.result()
        if decision.outcome == "approved":
            await self.send({"ok": True, "request_id": request.id, **request.result})
        else:
            await self.send({"ok": False, "request_id": request.id, "error": decision.outcome})

    async def op_session_start(self, message: dict[str, Any]) -> None:
        mapping, presets = self._mapping(message)
        request = self.broker.request_session(
            mapping=mapping,
            presets=presets,
            minutes=int(message.get("minutes") or 0),
            reason=self._reason(message),
            command=self._command(message),
            provenance=self.provenance,
        )
        await self.send(self._pending(request, excluded_per_run=request.summary["excluded_per_run"]))
        await self._attend(request)

    async def op_session_wait(self, message: dict[str, Any]) -> None:
        request = self.broker.state.requests.get(int(message.get("request_id") or 0))
        if request is None or request.kind != "session" or request.provenance.uid != self.provenance.uid:
            raise RequestError("unknown session request id")
        request.abandon_at = None
        await self.send({"ok": True, "request_id": request.id, "pending": request.pending})
        await self._attend(request)

    async def _attend(self, request: Request) -> None:
        if request.decided:
            await self._reply_decision(request)
            return
        if await self._wait_decision(request):
            await self._reply_decision(request)
        else:
            self.broker.state.mark_detached(request)
            self.broker.audit.event("detached", id=request.id, grace_until=request.abandon_at)

    async def op_session_end(self, message: dict[str, Any]) -> None:
        session = self.broker.end_session(str(message.get("session_id") or ""), by=f"pid {self.provenance.pid}", uid=self.provenance.uid)
        await self.send({"ok": True, "session_id": session.id})

    async def op_run(self, message: dict[str, Any]) -> None:
        reason = self._reason(message)
        command = self._command(message)
        session_id = message.get("session")
        if session_id:
            session, mapping = self.broker.resolve_run_in_session(str(session_id), list(message.get("with") or []), self.provenance.uid)
            self.broker.audit.event("run_auto_approved", session=session.id, vars=sorted(mapping), pid=self.provenance.pid, reason=reason or "(no reason given)")
            await self._start_run(mapping, session.id, reason, command)
            return
        mapping, presets = self._mapping(message)
        request = self.broker.request_run(mapping, presets, reason, command, self.provenance)
        await self.send(self._pending(request))
        if not await self._decided_or_withdrawn(request):
            return
        if request.decision.result().outcome != "approved":
            await self._reply_decision(request)
            return
        # The request's mapping, not the one asked for: a rename while it waited updated the request, which is what the approver saw.
        await self._start_run(dict(request.mapping), None, reason, command)

    async def _start_run(self, mapping: dict[str, str], session_id: str | None, reason: str | None, command: tuple[str, ...]) -> None:
        run = self.broker.state.start_run(mapping, session_id, reason, command, self.provenance)
        self.broker.audit.event("run_start", run=run.id, vars=sorted(mapping), session=session_id, pid=self.provenance.pid, command=" ".join(command))
        exit_code: int | None = None
        lingering_terminated: int | None = None
        try:
            await self.send({"ok": True, "run_id": run.id, "env": self.broker.env_for(mapping)})
            while True:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    note = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(note, dict) and note.get("op") == "run_done":
                    exit_code = note.get("exit_code")
                    lingering_terminated = note.get("lingering_terminated")
                    break
        finally:
            self.broker.state.end_run(run.id)
            self.broker.audit.event(
                "run_end",
                run=run.id,
                exit_code=exit_code if exit_code is not None else "client gone",
                lingering_terminated=lingering_terminated if lingering_terminated is not None else "unknown",
            )

    async def op_list(self, message: dict[str, Any]) -> None:
        await self.send({"ok": True, **self.broker.list_payload(for_uid=self.provenance.uid)})

    async def op_status(self, message: dict[str, Any]) -> None:
        await self.send({"ok": True, **self.broker.status_payload(for_uid=self.provenance.uid)})

    async def op_preset_validate(self, message: dict[str, Any]) -> None:
        try:
            raw = self.broker.validate_preset_yaml(str(message.get("yaml") or ""))
        except ConfigError as error:
            raise RequestError(str(error)) from error
        await self.send({"ok": True, "presets": sorted(raw)})

    async def op_preset_propose(self, message: dict[str, Any]) -> None:
        request = self.broker.request_preset(str(message.get("yaml") or ""), self._reason(message), self.provenance)
        await self.send(self._pending(request, presets=request.summary["presets"]))
        if not await self._decided_or_withdrawn(request):
            return
        await self._reply_decision(request)

    async def op_import(self, message: dict[str, Any]) -> None:
        secrets = message.get("secrets") or {}
        presets = message.get("presets") or {}
        if not isinstance(secrets, dict) or not isinstance(presets, dict):
            raise RequestError("import expects 'secrets' and 'presets' objects")
        request = self.broker.request_import(secrets, presets, self._reason(message), self.provenance)
        await self.send(self._pending(request))
        if not await self._decided_or_withdrawn(request):
            return
        if request.decision.result().outcome == "approved":
            renames = {**request.summary["reused"], **request.summary["renamed"]}
            request.result = {"added": request.summary["added"], "renames": renames, "presets": request.summary["presets"]}
        await self._reply_decision(request)
