import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from envh import platform
from envh.platform import PeerCredentials, PlatformError, parent_and_terminal, passwordless_sudo_entries, preflight_problems, terminal_path
from envh.server import hardening
from envh.server.hardening import HardeningError
from tests.conftest import needs_script, run_in_relayed_terminal

ROOT = Path(__file__).resolve().parents[1]

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


def test_a_command_line_shows_where_each_argument_starts_and_ends() -> None:
    argv = [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)", "--reason", "fix the build -- then rm -rf ~", "", "it's"]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline() == "started\n"
        shown = PeerCredentials(pid=process.pid, uid=os.getuid(), gid=os.getgid()).cmdline()
    finally:
        process.kill()
        process.wait()
    assert shown == shlex.join(argv) and shlex.split(shown) == argv
    assert "--reason 'fix the build -- then rm -rf ~' ''" in shown


def test_a_command_line_that_cannot_be_read_says_so() -> None:
    assert PeerCredentials(pid=2**31 - 1, uid=0, gid=0).cmdline() == "(command line unavailable)"


def test_a_parent_pid_is_read_past_a_process_name_with_spaces_and_parentheses(tmp_path: Path) -> None:
    oddly_named = tmp_path / "a) b ("
    oddly_named.symlink_to(sys.executable)
    process = subprocess.Popen([str(oddly_named), "-c", "import time; time.sleep(30)"])
    try:
        assert parent_and_terminal(process.pid)[0] == os.getpid()
    finally:
        process.kill()
        process.wait()


def test_a_terminal_is_found_by_its_device_number() -> None:
    leader, follower = os.openpty()
    try:
        assert terminal_path(os.fstat(follower).st_rdev) == Path(os.ttyname(follower))
    finally:
        os.close(leader)
        os.close(follower)
    with pytest.raises(PlatformError, match="no node in /dev"):
        terminal_path(os.makedev(4000, 7))


@needs_script
def test_ancestor_terminals_include_the_one_a_relay_reads_from(tmp_path: Path) -> None:
    report = tmp_path / "ancestors"
    program = "import os; from envh.platform import ancestor_terminals; print(*ancestor_terminals(os.getpid()))"
    outer, _ = run_in_relayed_terminal(f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} > {shlex.quote(str(report))}", tmp_path, {"PYTHONPATH": str(ROOT / "src")})
    assert str(outer) in report.read_text().split()


@pytest.mark.skipif(os.getuid() == 0, reason="as root every terminal and /dev/null have the same owner")
def test_the_console_refuses_while_a_terminal_it_was_started_from_is_not_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    leader, follower = os.openpty()
    own = Path(os.ttyname(follower))
    try:
        with open(follower, closefd=False) as console_input:
            monkeypatch.setattr(hardening.sys, "stdin", console_input)
            monkeypatch.setattr(hardening.os, "geteuid", lambda: own.stat().st_uid)
            monkeypatch.setattr(hardening, "ancestor_terminals", lambda pid: [own])
            hardening.assert_terminal_is_ours()
            foreign = Path("/dev/null")
            monkeypatch.setattr(hardening, "ancestor_terminals", lambda pid: [own, foreign])
            with pytest.raises(HardeningError, match=f"the terminal {foreign} is owned by uid {foreign.stat().st_uid}"):
                hardening.assert_terminal_is_ours()

            def unreadable(pid: int) -> list[Path]:
                raise PermissionError("/proc/1234/stat")

            monkeypatch.setattr(hardening, "ancestor_terminals", unreadable)
            with pytest.raises(HardeningError, match="can't check the terminals the console was started from"):
                hardening.assert_terminal_is_ours()
    finally:
        os.close(leader)
        os.close(follower)
