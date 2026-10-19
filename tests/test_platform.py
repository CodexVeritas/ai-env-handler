import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from envh import platform
from envh.platform import (
    PeerCredentials,
    PlatformError,
    doas_nopass_rules,
    parent_and_terminal,
    passwordless_sudo_entries,
    preflight_problems,
    preflight_warnings,
    sudo_defaults,
    terminal_path,
)
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
    monkeypatch.setattr(platform, "doas_problems", lambda username: [])
    monkeypatch.setattr(platform, "write_access_problems", lambda username, reported_groups: [])


def test_passwordless_sudo_entries() -> None:
    assert passwordless_sudo_entries(PASSWORD_ONLY_LISTING) == []
    assert passwordless_sudo_entries(NOPASSWD_LISTING) == ["(root) NOPASSWD: /usr/local/bin/backup"]
    assert len(passwordless_sudo_entries(NO_AUTHENTICATE_LISTING)) == 1


def test_a_primary_group_counts_as_a_root_granting_group(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {1000: "alice", 998: "docker", 27: "sudo"}
    monkeypatch.setattr(platform.pwd, "getpwnam", lambda username: SimpleNamespace(pw_gid=998))
    monkeypatch.setattr(platform.os, "getgrouplist", lambda username, primary_gid: [primary_gid, 27, 4242])
    monkeypatch.setattr(platform.grp, "getgrgid", lambda gid: SimpleNamespace(gr_name=names[gid]))
    assert platform.group_names_of("alice") == ["docker", "sudo"]
    assert platform.root_granting_groups_of("alice") == ["docker"]


def test_the_sudo_listing_is_asked_for_untranslated(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(command: list[str], **options: object) -> SimpleNamespace:
        seen.update(options)
        return SimpleNamespace(returncode=0, stdout=PASSWORD_ONLY_LISTING, stderr="")

    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setattr(platform.subprocess, "run", fake_run)
    assert platform.sudo_listing("alice") == PASSWORD_ONLY_LISTING
    environment = seen["env"]
    assert isinstance(environment, dict) and environment["LC_ALL"] == "C"


def test_sudo_defaults_are_read_across_wrapped_lines_and_stop_at_the_rules() -> None:
    listing = PASSWORD_ONLY_LISTING.replace("use_pty", "use_pty,\n    timestamp_type = global")
    assert sudo_defaults(listing) == ["env_reset", "mail_badpass", "secure_path=/usr/local/sbin\\:/usr/local/bin\\:/usr/sbin\\:/usr/bin", "use_pty", "timestamp_type=global"]
    assert sudo_defaults("User alice may run the following commands on host:\n    (ALL : ALL) ALL\n") == []


def test_sudo_tickets_shared_across_terminals_and_a_missing_secure_path_are_problems(safe_kernel: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "sudo_listing", lambda username: PASSWORD_ONLY_LISTING.replace("use_pty", "use_pty, !tty_tickets"))
    [problem] = preflight_problems("alice")
    assert "sudo's !tty_tickets lets every terminal of alice reuse a sudo password" in problem
    monkeypatch.setattr(platform, "sudo_listing", lambda username: PASSWORD_ONLY_LISTING.replace("secure_path", "env_keep"))
    [problem] = preflight_problems("alice")
    assert "sudo has no secure_path" in problem and "sudo /usr/local/sbin/envh-console" in problem


def test_doas_rules_that_skip_the_password_are_found_by_user_or_group_name_or_id() -> None:
    config_text = "\n".join(
        [
            "# permit nopass alice",
            "permit persist alice as root",
            "permit nopass keepenv :wheel",
            "permit nopass setenv { PATH=/usr/bin } alice cmd /usr/bin/true",
            "permit nopass bob",
            "permit nopass 1000",
            "permit nopass :27",
            "deny alice",
        ]
    )
    assert doas_nopass_rules(config_text, {"alice", ":alice", ":users"}) == ["permit nopass setenv { PATH=/usr/bin } alice cmd /usr/bin/true"]
    assert doas_nopass_rules(config_text, {"alice", ":wheel"}) == ["permit nopass keepenv :wheel", "permit nopass setenv { PATH=/usr/bin } alice cmd /usr/bin/true"]
    assert doas_nopass_rules(config_text, {"1000", ":27"}) == ["permit nopass 1000", "permit nopass :27"]


def test_doas_identities_name_the_account_and_its_groups_by_name_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform.pwd, "getpwnam", lambda username: SimpleNamespace(pw_uid=1000, pw_gid=1000))
    monkeypatch.setattr(platform.os, "getgrouplist", lambda username, primary_gid: [1000, 27])
    monkeypatch.setattr(platform, "group_names_of", lambda username: ["alice", "sudo"])
    assert platform.doas_identities("alice") == {"alice", "1000", ":alice", ":sudo", ":1000", ":27"}


def test_doas_nopass_rules_are_problems(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "doas.conf"
    monkeypatch.setattr(platform, "DOAS_CONFIG", config)
    monkeypatch.setattr(platform, "doas_identities", lambda username: {"alice", ":alice"})
    assert platform.doas_problems("alice") == []
    config.write_text("permit nopass alice\n")
    [problem] = platform.doas_problems("alice")
    assert "alice can run doas without a password" in problem and "\n  permit nopass alice" in problem


def test_a_socket_reached_through_a_reported_group_is_not_reported_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    daemon_socket = tmp_path / "docker.sock"
    daemon_socket.write_text("")
    monkeypatch.setattr(platform, "ROOT_DAEMON_SOCKETS", (daemon_socket,))
    monkeypatch.setattr(platform, "ROOT_RUN_FOLDERS", ())
    monkeypatch.setattr(platform, "writable_by", lambda username, paths: list(paths))
    socket_group = platform.group_name(daemon_socket.stat().st_gid)
    assert platform.write_access_problems("alice", [str(socket_group)]) == []
    assert len(platform.write_access_problems("alice", ["docker"] if socket_group != "docker" else ["lxd"])) == 1


def test_daemon_sockets_and_folders_root_runs_from_are_problems_when_the_user_can_write_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    daemon_socket = tmp_path / "docker.sock"
    daemon_socket.write_text("")
    monkeypatch.setattr(platform, "ROOT_DAEMON_SOCKETS", (daemon_socket,))
    monkeypatch.setattr(platform, "writable_by", lambda username, paths: [path for path in paths if path in (daemon_socket, Path("/usr/local/sbin"))])
    sockets, folders = platform.write_access_problems("alice", [])
    assert f"alice can use {daemon_socket}" in sockets and "can become root" in sockets
    assert "alice can write /usr/local/sbin, where root runs envh from" in folders and "sudo chmod go-w /usr/local/sbin" in folders

    def refused(username: str, paths: object) -> list[Path]:
        raise PlatformError("only root or alice can check what alice can write")

    monkeypatch.setattr(platform, "writable_by", refused)
    [problem] = platform.write_access_problems("alice", [])
    assert problem.startswith("could not check which root-owned sockets and folders alice can write")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write every file")
def test_writable_by_asks_the_kernel_as_the_account(tmp_path: Path) -> None:
    me = pwd.getpwuid(os.geteuid()).pw_name
    read_only = tmp_path / "read-only"
    read_only.write_text("")
    read_only.chmod(0o444)
    assert platform.writable_by(me, [tmp_path, read_only, tmp_path / "missing"]) == [tmp_path.resolve()]
    with pytest.raises(PlatformError, match="only root or root can check"):
        platform.writable_by("root", [tmp_path])


def test_keyboard_reading_groups_and_a_writable_uinput_are_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "session_type", lambda: "wayland")
    monkeypatch.setattr(platform, "group_names_of", lambda username: ["alice", "input"])
    monkeypatch.setattr(platform, "writable_by", lambda username, paths: list(paths))
    groups, uinput = preflight_warnings("alice")
    assert "alice is in group(s) input: programs you run can read every key you type" in groups
    assert "alice can write /dev/uinput" in uinput
    assert preflight_warnings(None) == []


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


def test_logins_allowed_to_ask_are_warned_about_root_and_key_reading_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    groups = {"alice": ["alice", "sudo"], "bob": ["bob", "docker", "input"]}
    monkeypatch.setattr(platform, "group_names_of", lambda username: groups[username])
    root_granting, key_reading = platform.listed_user_warnings(["alice", "bob"])
    assert root_granting.startswith("bob is in docker, which can become root") and "sudo gpasswd -d bob <group>" in root_granting
    assert key_reading.startswith("bob is in input")


def test_the_console_warns_when_it_runs_under_a_terminal_sharer_started_by_a_user(monkeypatch: pytest.MonkeyPatch) -> None:
    owners = {300: 1000, 400: 0}
    monkeypatch.setattr(hardening, "process_real_uid", lambda pid: owners[pid])
    monkeypatch.setattr(hardening, "ancestors", lambda pid: [(100, "sudo"), (200, "bash"), (400, "tmux: server")])
    assert hardening.shared_terminal_warning() is None
    monkeypatch.setattr(hardening, "ancestors", lambda pid: [(100, "sudo"), (200, "bash"), (300, "tmux: server")])
    warning = hardening.shared_terminal_warning()
    assert warning is not None and warning.startswith("The console runs inside tmux: server (pid 300): programs you run can see and type into it")

    def unreadable(pid: int) -> list[tuple[int, str]]:
        raise PermissionError("/proc/200/comm")

    monkeypatch.setattr(hardening, "ancestors", unreadable)
    warning = hardening.shared_terminal_warning()
    assert warning is not None and warning.startswith("Can't check whether the console runs inside tmux or screen")


def test_ancestors_are_named_nearest_first() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        nearest = platform.ancestors(process.pid)[0]
    finally:
        process.kill()
        process.wait()
    assert nearest == (os.getpid(), Path(f"/proc/{os.getpid()}/comm").read_text().strip())


def test_the_real_uid_is_read_from_the_process_status() -> None:
    assert platform.process_real_uid(os.getpid()) == os.getuid()
