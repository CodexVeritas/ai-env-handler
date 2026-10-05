"""Helpers shared by the client and the broker; standard library only."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

_UNSAFE_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f؜​-‏ -‮⁠-⁯﻿]")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PRESET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REMINDER_GAPS_SECONDS = (30, 30, 60, 180)
REMINDER_REPEAT_SECONDS = 300
PEEK_STEPS = ((40, 4), (32, 3), (20, 2), (12, 1))


def reminder_delays() -> Iterator[float]:
    """Seconds between reminders about a request still waiting: after 30 s, 1 min, 2 min and 5 min, then every 5 min."""
    yield from REMINDER_GAPS_SECONDS
    while True:
        yield REMINDER_REPEAT_SECONDS


def peek(value: str) -> str:
    """The first and last few characters of a value, to tell keys apart or match one with a provider's dashboard. Shorter
    values show fewer, so at most a fifth of a value is shown, and nothing of one under 12 characters."""
    shown = next((count for length, count in PEEK_STEPS if len(value) >= length), 0)
    return f"{value[:shown]}…{value[-shown:]}" if shown else "…"


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    return f"{peek(value)} ({len(value)} chars, id {digest})"


def printable(text: str) -> str:
    """Replace control, bidirectional and invisible formatting characters, so text supplied by a requester cannot add,
    erase, restyle or reorder console lines, or hide characters in them."""
    return _UNSAFE_CHARACTERS.sub("�", text)
