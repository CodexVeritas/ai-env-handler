import asyncio
import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import Harness, approve_next

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
    assert out.strip() == "sk-openai"
    assert "waiting for approval" in err
    await asyncio.sleep(0.05)
    assert harness.broker.state.active_runs() == []


async def test_session_start_then_run_and_end(control: Path, harness: Harness) -> None:
    task = asyncio.create_task(run_cli(control, "session", "start", "minibench", "--minutes", "30", "--reason", "e2e", "--quiet"))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0, err
    session_id = out.strip()
    code, out, err = await run_cli(control, "run", "--session", session_id, "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", "import os; print(sorted(k for k in os.environ if k in {'OPENAI_API_KEY','OPENROUTER_API_KEY'}))")
    assert code == 0, err
    assert out.strip() == "['OPENAI_API_KEY']"
    code, out, err = await run_cli(control, "run", "--", sys.executable, "-c", "import os; print(os.environ['OPENROUTER_API_KEY'])", env={"ENVH_SESSION": session_id})
    assert code == 0 and out.strip() == "sk-or-mini"
    code, out, err = await run_cli(control, "run", "--session", session_id, "--with", "DATABASE_URL", "--", "true")
    assert code == 5 and "not covered" in err
    code, out, err = await run_cli(control, "session", "end", session_id)
    assert code == 0
    code, out, err = await run_cli(control, "list")
    assert code == 0 and "minibench" in out and "sk-openai" not in out


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
    sleeper = "import subprocess; subprocess.Popen(['sleep', '2345.6'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('started')"
    task = asyncio.create_task(run_cli(control, "run", "--keep-background", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", sleeper))
    await approve_next(harness)
    code, out, err = await asyncio.wait_for(task, timeout=20)
    assert code == 0 and "terminated" not in err
    found = subprocess.run(["pgrep", "-f", "sleep 2345.6"], capture_output=True, text=True)
    assert found.returncode == 0
    for pid in found.stdout.split():
        subprocess.run(["kill", pid])
    assert any("run_end" in line and "lingering_terminated=0" in line for line in harness.echoed)


async def test_signal_killed_command_exits_like_a_shell(control: Path, harness: Harness) -> None:
    suicide = "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"
    task = asyncio.create_task(run_cli(control, "run", "--with", "OPENAI_API_KEY", "--", sys.executable, "-c", suicide))
    await approve_next(harness)
    code, _, err = await asyncio.wait_for(task, timeout=20)
    assert code == 128 + 15, err
