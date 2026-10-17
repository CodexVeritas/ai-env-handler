from __future__ import annotations

import re
from datetime import timedelta

MAX_SESSION = timedelta(hours=24)
# Bound the digit count: the hard cap is 24h, so no legitimate duration needs more than a few
# digits. Capping here keeps a hostile value (thousands of digits) from reaching int()/timedelta,
# where CPython's int->str limit raises ValueError and a huge amount overflows timedelta — both of
# which escaped DurationError before and surfaced as a broker "internal error" plus a traceback.
_PATTERN = re.compile(r"^(\d{1,8})([smh])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}


class DurationError(ValueError):
    pass


def parse_duration(text: str) -> timedelta:
    match = _PATTERN.match(text.strip())
    if not match:
        raise DurationError(f"invalid duration {text!r}: use a whole number (up to 8 digits) followed by s, m or h, like 30m")
    amount = int(match.group(1))
    if amount <= 0:
        raise DurationError(f"invalid duration {text!r}: must be greater than zero")
    try:
        return timedelta(**{_UNITS[match.group(2)]: amount})
    except OverflowError as error:
        raise DurationError(f"invalid duration {text!r}: too large") from error


def format_duration(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def cap_session(duration: timedelta) -> timedelta:
    return min(duration, MAX_SESSION)
