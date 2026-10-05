from datetime import timedelta

import pytest

from envh.core.durations import DurationError, cap_session, format_duration, parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30s", timedelta(seconds=30)), ("45m", timedelta(minutes=45)), ("2h", timedelta(hours=2)), (" 5m ", timedelta(minutes=5))],
)
def test_parse_valid(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "5", "5d", "0m", "-5m", "1.5h", "5 m", "m5"])
def test_parse_invalid(text: str) -> None:
    with pytest.raises(DurationError):
        parse_duration(text)


def test_format_round_trip() -> None:
    for text in ("30s", "45m", "2h", "90m"):
        assert parse_duration(format_duration(parse_duration(text))) == parse_duration(text)


def test_cap_session() -> None:
    assert cap_session(timedelta(hours=30)) == timedelta(hours=24)
    assert cap_session(timedelta(minutes=5)) == timedelta(minutes=5)


@pytest.mark.parametrize("text", ["9" * 6000 + "h", "9" * 500 + "h", "9" * 9 + "h"])
def test_absurdly_long_durations_are_clean_errors_not_crashes(text: str) -> None:
    # A huge digit string must not reach int()/timedelta (where CPython's int->str limit raises
    # ValueError and a large amount overflows timedelta); it is rejected as a DurationError instead.
    with pytest.raises(DurationError):
        parse_duration(text)
