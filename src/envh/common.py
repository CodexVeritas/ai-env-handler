"""Helpers shared by the client and the broker; standard library only."""

from __future__ import annotations

import hashlib
import re

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
PRESET_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    head = value[:4] if len(value) > 8 else value[:1]
    return f"{head}…{digest} ({len(value)} chars)"


def printable(text: str) -> str:
    """Replace control characters so text supplied by a requester cannot add, erase or restyle console lines."""
    return _CONTROL_CHARACTERS.sub("\ufffd", text)
