"""Client side of the broker's Unix-socket protocol. Standard library only."""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from envh.client.attention import Reminders, desktop_session

EXIT_USAGE = 2
EXIT_NO_BROKER = 3
EXIT_DENIED = 4
EXIT_REQUEST_ERROR = 5


class ClientError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_REQUEST_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Connection:
    def __init__(self, path: Path) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.connect(str(path))
        except OSError as error:
            raise ClientError(
                f"broker not running ({path}: {error.strerror}); start it with `sudo envh-console` in a separate terminal",
                EXIT_NO_BROKER,
            ) from error
        self._file = self._sock.makefile("rwb", buffering=0)

    def send(self, **message: Any) -> None:
        self._file.write(json.dumps(message).encode() + b"\n")

    def recv(self) -> dict[str, Any]:
        line = self._file.readline()
        if not line:
            raise ClientError("the broker closed the connection; see its console", EXIT_NO_BROKER)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ClientError("malformed reply from the broker")
        return reply

    def recv_ok(self) -> dict[str, Any]:
        reply = self.recv()
        if not reply.get("ok"):
            error = str(reply.get("error", "unknown error"))
            raise ClientError(error, EXIT_DENIED if error in ("denied", "withdrawn") else EXIT_REQUEST_ERROR)
        return reply

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def waiting_notice(reply: dict[str, Any], resume_hint: str | None) -> Iterator[None]:
    """Say the request waits for the console, and while it does, remind the human on the desktop when the broker's
    notify setting is on. Without a desktop session only the console's bell reminds them."""
    request_id = reply["request_id"]
    print(f"envh: waiting for approval in the envh console (request #{request_id})", file=sys.stderr, flush=True)
    if resume_hint:
        print(f"envh: if this times out, resume with: {resume_hint}", file=sys.stderr, flush=True)
    reminders = Reminders(request_id).start() if reply.get("notify") and desktop_session() else None
    try:
        yield
    finally:
        if reminders is not None:
            reminders.stop()
