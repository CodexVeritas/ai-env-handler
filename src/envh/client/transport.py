"""Client side of the broker's Unix-socket protocol. Standard library only."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

EXIT_USAGE = 2
EXIT_NO_BROKER = 3
EXIT_DENIED = 4
EXIT_REQUEST_ERROR = 5
NOTIFY_METHOD = ("--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications", "--method", "org.freedesktop.Notifications.Notify")
NOTIFY_HINTS = "{'sound-name': <'window-attention'>, 'urgency': <byte 1>}"


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


def waiting_notice(reply: dict[str, Any], resume_hint: str | None) -> None:
    request_id = reply["request_id"]
    print(f"envh: waiting for approval in the envh console (request #{request_id})", file=sys.stderr, flush=True)
    if resume_hint:
        print(f"envh: if this times out, resume with: {resume_hint}", file=sys.stderr, flush=True)
    if reply.get("notify"):
        notify_desktop(request_id)


def notify_desktop(request_id: int) -> None:
    """Show a desktop notification with a sound, so the human looks at the console. The text is fixed: nothing the
    requester wrote reaches it. Without a graphical session it does nothing; a failure is reported in one line."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    gdbus = shutil.which("gdbus")
    if gdbus is None:
        print("envh: notify is on, but gdbus is missing (package libglib2.0-bin), so no desktop notification was shown", file=sys.stderr)
        return
    body = f"Approve or deny request #{request_id} in the envh console."
    command = [gdbus, "call", "--session", *NOTIFY_METHOD, "--", "envh", "0", "dialog-password", "envh needs your passphrase", body, "[]", NOTIFY_HINTS, "-1"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"envh: could not show a desktop notification: {error}", file=sys.stderr)
        return
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[0] if result.stderr.strip() else f"exit code {result.returncode}"
        print(f"envh: could not show a desktop notification: {detail}", file=sys.stderr)
