from __future__ import annotations

import re
from datetime import timedelta

MAX_SESSION = timedelta(hours=24)
_PATTERN = re.compile(r"^(\d+)([smh])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


class DurationError(ValueError):
    pass


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if not match:
        raise DurationError(f"invalid duration {text!r}: use a whole number followed by s, m or h, like 30m")
    amount = int(match.group(1))
    if amount <= 0:
        raise DurationError(f"invalid duration {text!r}: must be greater than zero")
    return timedelta(**{_UNITS[match.group(2)]: amount})


def format_duration(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def cap_session(duration: timedelta) -> timedelta:
    return min(duration, MAX_SESSION)
