"""Desktop attention for a request that waits on the console: a notification with envh's own chime, repeated with
growing gaps until the request is answered, then removed. Standard library only.

The notification texts are fixed, so nothing the requester wrote reaches the desktop."""

from __future__ import annotations

import io
import itertools
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from envh.common import reminder_delays

NOTIFICATIONS = ("--dest", "org.freedesktop.Notifications", "--object-path", "/org/freedesktop/Notifications")
CHIME_FILE_NAME = "envh-chime-1.wav"
SAMPLE_RATE = 22050
CHIME_SECONDS = 1.3
CHIME_NOTES = ((659.25, 0.0), (830.61, 0.11), (987.77, 0.22), (1318.51, 0.33))
BELL_PARTIALS = ((1.0, 1.0, 4.0), (2.0, 0.35, 8.0), (3.0, 0.12, 13.0))


class NotifyError(RuntimeError):
    pass


def chime_wav() -> bytes:
    """envh's own sound, so it is not mistaken for another app's: a soft bell playing a rising E major arpeggio
    (E5, G#5, B5, E6). Each note is (frequency in Hz, start in seconds); each partial is (frequency ratio, loudness,
    decay per second)."""
    samples: list[float] = []
    for index in range(int(SAMPLE_RATE * CHIME_SECONDS)):
        moment = index / SAMPLE_RATE
        value = 0.0
        for frequency, start in CHIME_NOTES:
            elapsed = moment - start
            if elapsed < 0:
                continue
            attack = min(elapsed / 0.004, 1.0)
            for ratio, loudness, decay in BELL_PARTIALS:
                value += attack * loudness * math.exp(-decay * elapsed) * math.sin(2 * math.pi * frequency * ratio * elapsed)
        samples.append(value)
    peak = max(abs(sample) for sample in samples) or 1.0
    frames = b"".join(struct.pack("<h", round(sample / peak * 0.6 * 32767)) for sample in samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(frames)
    return buffer.getvalue()


def chime_file() -> Path | None:
    """The chime as a file the desktop can play, written once into the user's private runtime folder."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        return None
    path = Path(runtime) / CHIME_FILE_NAME
    if not path.is_file():
        temp_path = path.with_name(f".{CHIME_FILE_NAME}.{os.getpid()}")
        temp_path.write_bytes(chime_wav())
        os.replace(temp_path, path)
    return path


def desktop_session() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def readable_wait(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{round(seconds)} seconds" if seconds < 60 else f"{minutes} minute" + ("" if minutes == 1 else "s")


def gdbus_call(method: str, *arguments: str) -> str:
    gdbus = shutil.which("gdbus")
    if gdbus is None:
        raise NotifyError("gdbus is missing (package libglib2.0-bin)")
    command = [gdbus, "call", "--session", *NOTIFICATIONS, "--method", f"org.freedesktop.Notifications.{method}", "--", *arguments]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NotifyError(str(error)) from error
    if result.returncode != 0:
        raise NotifyError(result.stderr.strip().splitlines()[0] if result.stderr.strip() else f"gdbus exit code {result.returncode}")
    return result.stdout


def notify(request_id: int, waited: float, replaces: int) -> int:
    """Show the notification, or update the one with id `replaces`, and return its id."""
    if waited < 1:
        body = f"Approve or deny request #{request_id} in the envh console."
    else:
        body = f"Request #{request_id} has been waiting {readable_wait(waited)}. Approve or deny it in the envh console."
    try:
        chime = chime_file()
    except OSError:
        chime = None
    sound = f"'sound-file': <'{chime}'>" if chime is not None and "'" not in str(chime) else "'sound-name': <'window-attention'>"
    reply = gdbus_call("Notify", "envh", str(replaces), "dialog-password", "envh needs your passphrase", body, "[]", "{" + sound + ", 'urgency': <byte 1>}", "-1")
    found = re.search(r"uint32 (\d+)", reply)
    return int(found.group(1)) if found else replaces


class Reminders:
    """Notifies at once, then again after each of reminder_delays() while the request waits. stop() ends the
    reminders and removes the notification, since the request no longer needs anyone."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        self.notification_id = 0
        self.error: str | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> Reminders:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=6)
        if self.notification_id:
            try:
                gdbus_call("CloseNotification", str(self.notification_id))
            except NotifyError:
                pass

    def _run(self) -> None:
        started = time.monotonic()
        for delay in itertools.chain([0.0], reminder_delays()):
            if self._stopped.wait(delay):
                return
            try:
                self.notification_id = notify(self.request_id, time.monotonic() - started, self.notification_id)
            except NotifyError as error:
                self.error = str(error)
                print(f"envh: could not show a desktop notification: {error}", file=sys.stderr, flush=True)
                return
