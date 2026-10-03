"""Helpers shared by the client and the broker; standard library only."""

from __future__ import annotations

import hashlib


def fingerprint(value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()[:6]
    head = value[:4] if len(value) > 8 else value[:1]
    return f"{head}…{digest} ({len(value)} chars)"
