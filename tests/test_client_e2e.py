import asyncio
import errno
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from envh.client.commands import CONSOLE_COMMANDS
from envh.core.config import SecretEntry
from envh.server.console.home import COMMANDS
from tests.conftest import Harness, approve_next, make_auto

ROOT = Path(__file__).resolve().parents[1]


async def run_cli(socket_path: Path, *args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "envh", "--socket", str(socket_path), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), **(env or {})},
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def test_run_prompts_then_injects_env(control: Path, harness: Harness) -> None:
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--reason", "e2e", "--", sys.executable, "-c", "import os; print(os.environ['OPENAI_API_KEY']); raise SystemExit(7)"))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 7
    assert out.strip() == "[envh:OPENAI_API_KEY]"
    assert "waiting for approval" in err
    await asyncio.sleep(0.05)
    assert harness.broker.state.active_runs() == []


async def test_one_command_with_a_preset_needs_no_session(control: Path, harness: Harness) -> None:
    printer = "import os; print(os.environ['DATABASE_URL'], os.environ['OPENAI_API_KEY'])"
    task = asyncio.create_task(run_cli(control, "run", "--preset", "dbwork", "--reason", "one-off", "--", sys.executable, "-c", printer))
    for _ in range(50):
        if harness.broker.state.pending():
            break
        await asyncio.sleep(0.01)
    assert [request.kind for request in harness.broker.state.pending()] == ["run"]
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err
    assert out.split() == ["[envh:DATABASE_URL]", "[envh:OPENAI_API_KEY]"]
    assert harness.broker.state.live_sessions() == []


async def test_a_session_start_that_timed_out_resumes_with_the_printed_command(control: Path, harness: Harness) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "envh", "--socket", str(control), "session", "start", "team", "--minutes", "30", "--reason", "e2e",
        stderr=asyncio.subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert process.stderr is not None
    hint = ""
    while "resume with:" not in hint:
        hint = (await asyncio.wait_for(process.stderr.readline(), timeout=20)).decode()
    process.kill()
    await process.wait()
    resume_command = hint.split("resume with: ", 1)[1].split()
    assert resume_command[:3] == ["envh", "session", "wait"]
    task = asyncio.create_task(run_cli(control, *resume_command[1:], "--quiet"))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err
    assert out.strip() == harness.broker.state.live_sessions()[0].id


async def test_session_start_then_run_and_end(control: Path, harness: Harness) -> None:
    task = asyncio.create_task(run_cli(control, "session", "start", "team", "--minutes", "30", "--reason", "e2e", "--quiet"))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err
    session_id = out.strip()
    code, out, err = await run_cli(control, "run", "--session", session_id, "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k in {'OPENAI_API_KEY','OPENROUTER_API_KEY'}))")
    assert code == 0, err
    assert out.strip() == "['OPENAI_API_KEY']"
    code, out, err = await run_cli(control, "run", "--session", session_id, "--", sys.executable, "-c", "import os; print(os.environ['OPENROUTER_API_KEY'])")
    assert code == 0 and out.strip() == "[envh:OPENROUTER_API_KEY]"
    code, out, err = await run_cli(control, "run", "--session", session_id, "--with", "DATABASE_URL", "--", "true")
    assert code == 5 and "not covered" in err
    code, out, err = await run_cli(control, "session", "end")
    assert code == 2 and "required: session_id" in err
    code, out, err = await run_cli(control, "session", "end", session_id)
    assert code == 0
    code, out, err = await run_cli(control, "list")
    assert code == 0 and "team" in out and "sk-openai" not in out



async def test_presets_combine_on_the_command_line(control: Path, harness: Harness) -> None:
    show = "import os; print(sorted(k for k in os.environ if k in {'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'DATABASE_URL'}))"
    task = asyncio.create_task(run_cli(control, "run", "--preset", "team,dbwork", "--", sys.executable, "-c", show))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err
    assert out.strip() == "['DATABASE_URL', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY']"
    task = asyncio.create_task(run_cli(control, "session", "start", "team", "dbwork", "--minutes", "30", "--quiet"))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0 and "not covered by sessions: DATABASE_URL" in err, err
    code, out, err = await run_cli(control, "list")
    assert code == 0 and "team, dbwork" in out

async def test_denied_and_no_broker(control: Path, harness: Harness, tmp_path: Path) -> None:
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", "true"))
    for _ in range(50):
        if harness.broker.state.pending():
            break
        await asyncio.sleep(0.01)
    harness.broker.deny(harness.broker.state.pending()[0])
    code, _, err = await asyncio.wait_for(task, timeout=20)
    assert code == 4 and "denied" in err
    code, _, err = await run_cli(tmp_path / "missing.sock", "list")
    assert code == 3 and "broker not running" in err


async def test_background_processes_are_terminated_after_the_run(control: Path, harness: Harness) -> None:
    sleeper = "import subprocess, sys; subprocess.Popen(['sleep', '1234.5'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('started')"
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", sleeper))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0 and out.strip() == "started", err
    assert "terminated 1 process" in err
    assert subprocess.run(["pgrep", "-f", "sleep 1234.5"], capture_output=True).returncode == 1
    assert any("run_end" in line and "lingering_terminated=1" in line for line in harness.echoed)


async def test_keep_background_leaves_processes_alone(control: Path, harness: Harness) -> None:
    sleeper = "import os, subprocess; subprocess.Popen(['sleep', '2345.6'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('started', os.environ['OPENAI_API_KEY'])"
    task = asyncio.create_task(run_cli(control, "run", "--keep-background", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", sleeper))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0 and "terminated" not in err
    assert out.strip() == "started sk-openai"
    found = subprocess.run(["pgrep", "-f", "sleep 2345.6"], capture_output=True, text=True)
    assert found.returncode == 0
    for pid in found.stdout.split():
        subprocess.run(["kill", pid])
    assert any("run_end" in line and "lingering_terminated=0" in line for line in harness.echoed)


async def test_values_are_hidden_in_what_the_command_prints(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    printer = "import os, sys, time; key = os.environ['OPENAI_API_KEY']; print('key', key); print(key[:4], end='', file=sys.stderr, flush=True); time.sleep(0.5); print(key[4:], file=sys.stderr)"
    code, out, err = await run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", printer)
    assert code == 0, err
    assert out == "key [envh:OPENAI_API_KEY]\n"
    assert "\n[envh:OPENAI_API_KEY]\n" in err and "sk-openai" not in err
    assert "envh: hid the value of OPENAI_API_KEY in the output" in err


async def test_a_terminal_stays_a_terminal(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    controller, terminal = os.openpty()
    printer = "import os, sys; print(os.isatty(1), os.isatty(2), os.environ['OPENAI_API_KEY']); sys.stdin.readline()"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "envh", "--socket", str(control), "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", printer,
        stdin=asyncio.subprocess.PIPE, stdout=terminal, stderr=terminal, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    os.close(terminal)
    output = b""
    try:
        while b"True True [envh:OPENAI_API_KEY]\r\n" not in output:
            chunk = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, read_terminal, controller), timeout=10)
            assert chunk, output
            output += chunk
        await asyncio.wait_for(process.communicate(b"\n"), timeout=20)
        while chunk := read_terminal(controller):
            output += chunk
    finally:
        os.close(controller)
    assert process.returncode == 0 and b"sk-openai" not in output


def read_terminal(controller: int) -> bytes:
    try:
        return os.read(controller, 4096)
    except OSError as error:
        if error.errno != errno.EIO:
            raise
        return b""


async def test_output_shows_while_the_command_runs(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    prompt = "import sys; sys.stdout.write('Continue? '); sys.stdout.flush(); sys.stdin.readline(); print('done')"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "envh", "--socket", str(control), "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", prompt,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert process.stdout is not None
    assert await asyncio.wait_for(process.stdout.readexactly(len("Continue? ")), timeout=10) == b"Continue? "
    out, err = await asyncio.wait_for(process.communicate(b"\n"), timeout=20)
    assert process.returncode == 0, err
    assert out == b"done\n"


async def test_a_reader_that_stops_early_ends_the_run(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    endless = "import itertools; [print(number) for number in itertools.count()]"
    reader, writer = os.pipe()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "envh", "--socket", str(control), "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", endless,
        stdout=writer, stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    os.close(writer)
    first = await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, os.read, reader, 2), timeout=20)
    assert first.startswith(b"0")
    os.close(reader)
    assert await asyncio.wait_for(process.wait(), timeout=20) != 0


async def test_a_leftover_process_holding_the_output_does_not_keep_the_run_open(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    marked = "import subprocess; subprocess.Popen(['sleep', '3456.7']); print('started')"
    code, out, err = await asyncio.wait_for(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", marked), timeout=20)
    assert code == 0 and out == "started\n" and "terminated 1 process" in err, err
    unmarked = "import subprocess; subprocess.Popen(['sleep', '4567.8'], env={}); print('started')"
    try:
        code, out, err = await asyncio.wait_for(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", unmarked), timeout=20)
        assert code == 0 and out == "started\n", err
    finally:
        found = subprocess.run(["pgrep", "-f", "sleep 4567.8"], capture_output=True, text=True)
        for pid in found.stdout.split():
            subprocess.run(["kill", pid])


async def test_signal_killed_command_exits_like_a_shell(control: Path, harness: Harness) -> None:
    suicide = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", suicide))
    await approve_next(harness)
    code, _, err = await asyncio.wait_for(task, timeout=20)
    assert code == 128 + 15, err


async def test_manage_lists_console_commands_and_sees_the_running_console(control: Path) -> None:
    code, out, err = await run_cli(control, "manage")
    assert code == 0, err
    assert "/keys" in out and "/presets" in out and "/settings" in out
    assert "✓ The console is running." in out
    assert "sudo envh-console" not in out


async def test_manage_says_how_to_start_a_stopped_console(tmp_path: Path) -> None:
    code, out, err = await run_cli(tmp_path / "missing.sock", "manage")
    assert code == 0, err
    assert "! The console isn't running." in out
    assert "sudo envh-console" in out


def test_manage_names_only_commands_the_console_knows() -> None:
    known = {"/" + command.name for command in COMMANDS}
    for command, _purpose in CONSOLE_COMMANDS:
        assert command in known, command


async def test_list_shows_each_key_with_its_description(control: Path, harness: Harness) -> None:
    settings = harness.broker.config.settings
    described = replace(settings.secrets.get("DATABASE_URL", SecretEntry()), description="Production database, read-only user")
    harness.broker.save_settings(replace(settings, secrets={**settings.secrets, "DATABASE_URL": described}))
    code, out, err = await run_cli(control, "list")
    assert code == 0, err
    assert "DATABASE_URL" in out and "Production database, read-only user" in out
    assert "postgres://x" not in out


async def test_the_approver_sees_where_each_argument_of_the_requester_starts_and_ends(control: Path, harness: Harness) -> None:
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--reason", "two words -- three", "--", sys.executable, "-c", "pass"))
    for _ in range(200):
        pending = harness.broker.state.pending()
        if pending:
            break
        await asyncio.sleep(0.05)
    [request] = pending
    assert "--reason 'two words -- three' -- " in request.provenance.cmdline
    harness.broker.approve(request)
    code, _, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err


async def test_keys_that_need_no_approval_run_at_once_and_show_in_list(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    code, out, err = await run_cli(control, "run", "--with", "OPENAI_API_KEY", "--reason", "free", "--", sys.executable, "-c", "import os; print(os.environ['OPENAI_API_KEY'])")
    assert code == 0, err
    assert out.strip() == "[envh:OPENAI_API_KEY]" and "waiting for approval" not in err
    code, out, err = await run_cli(control, "session", "start", "dbwork", "--minutes", "5", "--reason", "free", "--quiet")
    assert code == 0, err
    assert harness.broker.state.session(out.strip()).mapping == {"OPENAI_API_KEY": "OPENAI_API_KEY"}
    assert "not covered by sessions: DATABASE_URL" in err and "waiting for approval" not in err
    code, out, err = await run_cli(control, "list")
    assert code == 0, err
    assert re.search(r"^  OPENAI_API_KEY +auto ", out, re.MULTILINE)
    assert re.search(r"OPENAI_API_KEY +<- OPENAI_API_KEY   \[auto\]", out)
