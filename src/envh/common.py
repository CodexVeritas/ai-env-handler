"""Helpers shared by the client and the broker; standard library only."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
REMINDER_GAPS_SECONDS = (30, 30, 60, 180)
REMINDER_REPEAT_SECONDS = 300


def reminder_delays() -> Iterator[float]:
    """Seconds between reminders about a request still waiting: after 30 s, 1 min, 2 min and 5 min, then every 5 min."""
    yield from REMINDER_GAPS_SECONDS
    while True:
        yield REMINDER_REPEAT_SECONDS


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    head = value[:4] if len(value) > 8 else value[:1]
    return f"{head}…{digest} ({len(value)} chars)"


def printable(text: str) -> str:
    """Replace control characters so text supplied by a requester cannot add, erase or restyle console lines."""
    return _CONTROL_CHARACTERS.sub("\ufffd", text)
