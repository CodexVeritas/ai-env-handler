import pytest

from envh import platform
from envh.platform import PlatformError, passwordless_sudo_entries, preflight_problems

PASSWORD_ONLY_LISTING = """Matching Defaults entries for alice on host:
    env_reset, mail_badpass, secure_path=/usr/local/sbin\\:/usr/local/bin\\:/usr/sbin\\:/usr/bin, use_pty

User alice may run the following commands on host:
    (ALL : ALL) ALL
"""
NOPASSWD_LISTING = PASSWORD_ONLY_LISTING + "    (root) NOPASSWD: /usr/local/bin/backup\n"
NO_AUTHENTICATE_LISTING = PASSWORD_ONLY_LISTING.replace("use_pty", "use_pty, !authenticate")


def fail_listing(username: str) -> str:
    raise PlatformError("sudo: a password is required")


@pytest.fixture
def safe_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "legacy_tiocsti_enabled", lambda: False)
    monkeypatch.setattr(platform, "yama_ptrace_scope", lambda: 1)
    monkeypatch.setattr(platform, "root_granting_groups_of", lambda username: [])


def test_passwordless_sudo_entries() -> None:
    assert passwordless_sudo_entries(PASSWORD_ONLY_LISTING) == []
    assert passwordless_sudo_entries(NOPASSWD_LISTING) == ["(root) NOPASSWD: /usr/local/bin/backup"]
    assert len(passwordless_sudo_entries(NO_AUTHENTICATE_LISTING)) == 1


def test_preflight_lists_passwordless_sudo_rules(safe_kernel: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "sudo_listing", lambda username: NOPASSWD_LISTING)
    [problem] = preflight_problems("alice")
    assert "without a password" in problem and "(root) NOPASSWD: /usr/local/bin/backup" in problem
    monkeypatch.setattr(platform, "sudo_listing", lambda username: PASSWORD_ONLY_LISTING)
    assert preflight_problems("alice") == []


def test_preflight_reports_unreadable_sudo_rules_and_skips_them_without_an_invoking_user(safe_kernel: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "sudo_listing", fail_listing)
    [problem] = preflight_problems("alice")
    assert "could not check the sudo rules of alice" in problem and "a password is required" in problem
    assert preflight_problems(None) == []
