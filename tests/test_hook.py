import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import Harness, make_auto

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "claude" / "hooks" / "envh_ask.py"
CURSOR_HOOK = json.loads((ROOT / "cursor" / "hooks.snippet.json").read_text())["hooks"]["beforeShellExecution"][0]
NO_BROKER = "/nonexistent/envh/ctl.sock"


def claude_payload(command: str, tool: str = "Bash") -> dict:
    return {"tool_name": tool, "tool_input": {"command": command}, "hook_event_name": "PreToolUse"}


def run_hook_raw(payload: dict) -> dict | None:
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True, env={**os.environ, "ENVH_SOCKET": NO_BROKER})
    return json.loads(result.stdout) if result.stdout.strip() else None


def run_hook(command: str, tool: str = "Bash") -> dict | None:
    output = run_hook_raw(claude_payload(command, tool))
    return None if output is None else output["hookSpecificOutput"]


def run_cursor_hook(command: str) -> dict | None:
    return run_hook_raw({"hook_event_name": "beforeShellExecution", "command": command, "cwd": "/project", "sandbox": False})


DECISIONS = pytest.mark.parametrize(
    ("command", "expected"),
    [
        # only invocations that make the console ask for the vault passphrase
        ("envh session start research --minutes 60 --reason x", "ask"),
        ("envh preset propose draft.yaml", "ask"),
        ("envh import ~/code", "ask"),
        ("envh run --with OPENAI_API_KEY -- python x.py", "ask"),
        ("envh run --with A python x.py --session abc", "ask"),
        ("PYTHONPATH=. envh run --with KEY -- python x.py", "ask"),
        ("envh --socket /nonexistent/envh-dev/ctl.sock session start p --minutes 5", "ask"),
        ("envh --socket=/nonexistent/envh-dev/ctl.sock run --with A -- true", "ask"),
        ("envh run --session abc --reason y -- python x.py", None),
        # envh doesn't read ENVH_SESSION, so only --session puts a run in a session
        ("ENVH_SESSION=abc envh run -- python x.py", "ask"),
        ("export ENVH_SESSION=abc && envh run -- python x.py", "ask"),
        ("envh import --dry-run ~/code", None),
        ("envh session start --help", None),
        ("envh list", None),
        ("envh status && git status", None),
        ("envh session wait 7", None),
        ("envh session end abc", None),
        ("envh preset validate d.yaml", None),
        ("envh scan ~/code --json", None),
        ("envh --help", None),
        ("envh frobnicate", None),
        # chaining and shell constructs
        ("cd repo && envh session start --with A --minutes 5", "ask"),
        ("true&&envh run --with A -- true", "ask"),
        ("false || envh session start p --minutes 5", "ask"),
        ("git pull; envh run -- python x.py", "ask"),
        ("cd repo\nenvh session start p --minutes 5", "ask"),
        ("envh \\\n  session start p --minutes 5", "ask"),
        ("(envh session start p --minutes 5)", "ask"),
        ("{ envh session start p --minutes 5; }", "ask"),
        ("if envh run --with A -- true; then echo ok; fi", "ask"),
        ("id=$(envh session start p --minutes 5 --quiet)", "ask"),
        ('id="$(envh session start p --minutes 5 --quiet)"', "ask"),
        ("id=`envh session start p --minutes 5 --quiet`", "ask"),
        ("echo $(cat $(envh session start p --minutes 5 --quiet))", "ask"),
        ("envh run --session abc -- python x.py && envh run --with B -- true", "ask"),
        ("envh run --with B -- true | tee log; envh list", "ask"),
        ("envh run --with B -- true 2>&1 > log.txt", "ask"),
        ("cat > notes.md <<'EOF'\nenvh run --with A -- true\nEOF\nenvh session start p --minutes 5", "ask"),
        ("cat <<\\EOF > notes.md\nDon't run envh session start\nEOF\nenvh session start p --minutes 5", "ask"),
        ('x=$(echo ")"); envh session start p --minutes 5', "ask"),
        ("diff <(envh run --with A -- cat x) y", "ask"),
        ("echo $((1<<2))\nenvh session start p --minutes 5", "ask"),
        ("cat > notes.md <<EOF\nRun `envh session start p --minutes 5` first\nEOF", "ask"),
        # quoting that cannot be read falls back to looking for an envh request in the text
        ('echo "unclosed; envh session start p --minutes 5', "ask"),
        ('echo "unclosed; envh list', None),
        # disguises
        ("/usr/local/bin/envh run --with A -- true", "ask"),
        ("./envh session start p --minutes 5", "ask"),
        ("bash -c 'envh session start p --minutes 5'", "ask"),
        ('sh -c "envh run --with A -- true"', "ask"),
        ("bash -lc 'cd repo && envh session start p --minutes 5'", "ask"),
        ('eval "envh session start p --minutes 5"', "ask"),
        ("timeout 600 envh session start p --minutes 5", "ask"),
        ("timeout -s KILL 600 envh session start p --minutes 5", "ask"),
        ("nice -n 10 envh run --with A -- true", "ask"),
        ("xargs -n 1 envh session start --minutes 5 < presets.txt", "ask"),
        ("command -v envh", None),
        ("nohup envh run --with A -- true &", "ask"),
        ("env -i PATH=/usr/bin envh session start p --minutes 5", "ask"),
        ("uv run envh session start p --minutes 5", "ask"),
        ("uv run envh run --session abc -- python x.py", None),
        # mentions are not invocations
        ("echo envh", None),
        ('echo "envh session start p --minutes 5"', None),
        ("grep -rn 'envh run --with' README.md", None),
        ('git commit -m "Ask before envh session start; skip envh list"', None),
        ("git commit -m \"$(cat <<'EOF'\nDon't forget envh session start\nEOF\n)\"", None),
        ("cat > README.md <<'EOF'\nRun this:\n  envh session start p --minutes 5\nDon't forget the reason.\nEOF", None),
        ("cat <<-EOF > notes.md\n\tenvh import ~/code\n\tEOF", None),
        ("cat <<\\EOF > notes.md\nRun envh session start p --minutes 5\nEOF", None),
        ("cat > notes.md <<EOF\n  EOF\nenvh session start p --minutes 5\nEOF", None),
        ("ls  # then envh session start p --minutes 5", None),
        ("rg envh src/ | head", None),
        ("bash scripts/envh-session.sh", None),
        ("python -c \"import subprocess; subprocess.run(['envh','session','start','p','--minutes','5'])\"", None),
        # the --session must belong to this invocation, not to the command after -- or to a later segment
        ("envh run --with A -- python x.py --session abc", "ask"),
        ("envh run --session abc -- python x.py --other", None),
        ("envh run --with A -- true; echo --session", "ask"),
        ('envh run --with A --reason "no --session here" -- true', "ask"),
        ("envh run --session-file x --with A -- true", "ask"),
        ("envh run --session=abc -- true", None),
        # innocents
        ("ls /opt/envh/bin", None),
        ("git status", None),
        ("python -c 'print(1)'", None),
        # --- regressions for red-team hook-bypass findings (each really starts a passphrase request) ---
        # runner value-options must not be read as the command name (uv/poetry)
        ("uv run --with rich envh run --reason r -- echo hi", "ask"),
        ("uv run --with=rich envh run --reason r -- echo hi", "ask"),
        ("uv run --python 3.12 envh session start p --minutes 5", "ask"),
        ("uv run -p 3.13 envh run --reason r -- echo hi", "ask"),
        ("uv run --project /tmp envh preset propose d.yaml", "ask"),
        ("uv run --directory /tmp envh import ~/code", "ask"),
        ("poetry run --directory /tmp envh run --with A -- true", "ask"),
        # a runner value-option before a NON-envh command must still not over-ask
        ("uv run --with rich python script.py", None),
        ("uv run --python 3.12 pytest -q", None),
        # an empty --session is not a session -> session-less run prompts
        ("envh run --session= -- true", "ask"),
        ("envh run --session '' -- true", "ask"),
        # env --split-string / -S runs a nested command the hook must read
        ('env --split-string="envh run --reason r -- echo hi"', "ask"),
        ("env -S'envh session start p --minutes 5'", "ask"),
        ("env --split-string='envh import ~/code'", "ask"),
        # value-taking shell options before -c must not hide the script
        ("bash -O extglob -c 'envh session start p --minutes 5'", "ask"),
        ("bash --rcfile /tmp/rc -c 'envh run --with A -- true'", "ask"),
        ("bash -o pipefail -c 'envh run --with A -- true'", "ask"),
        ("bash --init-file /tmp/i -c 'envh import ~/code'", "ask"),
        # eval drops a leading -- before running the rest
        ("eval -- 'envh run --reason r -- echo hi'", "ask"),
        ("eval -- 'envh session start p --minutes 5'", "ask"),
        # nesting deeper than the parser's cap falls back to the word-match (still asks)
        ("eval eval eval eval envh run --reason r -- echo hi", "ask"),
        ("bash -c 'eval eval eval envh session start p --minutes 5'", "ask"),
        # ...but benign deep nesting without an envh request does not ask
        ("eval eval eval eval echo hi", None),
        ("bash -c 'eval eval eval echo hi'", None),
        # the word-match fallback also catches an --socket option before the subcommand
        ("eval eval eval eval envh --socket=/tmp/x run --with A -- true", "ask"),
    ],
)


@DECISIONS
def test_hook_decisions(command: str, expected: str | None) -> None:
    output = run_hook(command)
    if expected is None:
        assert output is None, output
    else:
        assert output is not None, command
        assert output["permissionDecision"] == expected
        assert output["hookEventName"] == "PreToolUse"


@DECISIONS
def test_cursor_hook_decisions_and_matcher(command: str, expected: str | None) -> None:
    output = run_cursor_hook(command)
    if expected is None:
        assert output is None, output
    else:
        assert output is not None, command
        assert output["permission"] == expected
        assert output["user_message"] == output["agent_message"] != ""


def test_hook_falls_back_to_the_word_match_when_nested_too_deeply_to_parse() -> None:
    nested = "echo " + "$(" * 3000 + "{} ls" + ")" * 3000
    assert run_hook(nested.format("true")) is None
    output = run_hook(nested.format("envh session start p --minutes 5"))
    assert output is not None
    assert output["permissionDecision"] == "ask"


def test_hook_ignores_other_tools() -> None:
    assert run_hook("envh session start p --minutes 5", tool="Read") is None


# --- 2026-10-19 red-team regressions (hook-bypass findings, MEDIUM; the hook is not the boundary) ---

def test_ansi_c_quoted_command_name_is_decoded_so_envh_is_not_hidden() -> None:
    # bash resolves $'\x65nvh' to the command name "envh"; the hook must decode the $'...' escapes too,
    # or an envh passphrase request hides from its attention.
    output = run_hook("$'\\x65nvh' preset propose draft.yaml --reason x")
    assert output is not None and output["permissionDecision"] == "ask"


def test_added_exec_wrappers_and_runners_do_not_hide_envh() -> None:
    for command in (
        "ionice -c3 envh preset propose d.yaml",
        "taskset -c 0 envh preset propose d.yaml",
        "rye run envh preset propose d.yaml",
    ):
        output = run_hook(command)
        assert output is not None and output["permissionDecision"] == "ask", command


def test_cursor_hook_has_no_command_text_matcher_and_acts_on_a_reconstructed_name() -> None:
    assert "matcher" not in CURSOR_HOOK, "a command-text matcher skips reconstructed envh names; the hook must run on every beforeShellExecution"
    output = run_cursor_hook('e""nvh preset propose draft.yaml')
    assert output is not None and output["permission"] == "ask"


async def hook_against_broker(socket_path: Path, payload: dict) -> dict | None:
    """The hook's output with the broker listening, so it can answer the hook while the hook waits."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(HOOK), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env={**os.environ, "ENVH_SOCKET": str(socket_path)}
    )
    stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), timeout=20)
    return json.loads(stdout) if stdout.strip() else None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("envh run --with OPENAI_API_KEY --reason x -- python x.py", None),
        ("envh run --with=OPENAI_API_KEY --keep-background -- python x.py", None),
        ("envh session start dbwork --minutes 5 --reason x --quiet", None),
        ("cd repo && envh run --with OPENAI_API_KEY -- true; envh list", None),
        ("envh run --preset dbwork -- python x.py", "ask"),
        ("envh run --with OPENAI_API_KEY,DATABASE_URL -- true", "ask"),
        ("envh run --with OPENAI_API_KEY -- true && envh run --with DATABASE_URL -- true", "ask"),
        ("envh session start team --minutes 5", "ask"),
        ("envh run --wi OPENAI_API_KEY -- true", "ask"),
        ("envh run --preset nope -- true", "ask"),
        ("envh preset propose draft.yaml", "ask"),
        # options bash rewrites before envh reads them
        ("envh run --with OPENAI_API_KEY$(printf ,DATABASE_URL) -- true", "ask"),
        ('envh run --with "OPENAI_API_KEY$(printf ,DATABASE_URL)" -- true', "ask"),
        ("envh run --with OPENAI_API_KEY $(printf -- --with=DATABASE_URL) -- true", "ask"),
        ("envh run --with OPENAI_API_KEY $EXTRA -- true", "ask"),
        ("envh run --with OPENAI_API_KEY --reason $WHY -- true", "ask"),
        ("envh session start dbwork --minutes `echo 5`", "ask"),
        ('eval "envh run --with OPENAI_API_KEY$(printf ,DATABASE_URL) -- true"', "ask"),
        ('envh run --with OPENAI_API_KEY -- python x.py --since "$(date +%F)" $HOME', None),
        # a $ bash leaves as text
        ('envh run --with OPENAI_API_KEY --reason "costs ~\\$1-2" -- python x.py > /tmp/x.log 2>&1; echo "exit: $?"', None),
        ("envh run --with OPENAI_API_KEY --reason 'costs ~$2' -- python x.py", None),
        ("envh run --with OPENAI_API_KEY --reason \\$2 -- python x.py", None),
        ('envh run --with OPENAI_API_KEY --reason "$WHY" -- true', "ask"),
        ('eval "envh run --with OPENAI_API_KEY --reason \\$R -- true"', "ask"),
        # a broker the hook can't be sure the command reaches
        ("ENVH_SOCKET=/tmp/strict.sock envh run --with OPENAI_API_KEY -- true", "ask"),
        ("export ENVH_SOCKET=/tmp/strict.sock && envh run --with OPENAI_API_KEY -- true", "ask"),
        ("env ENVH_SOCKET=/tmp/strict.sock envh run --with OPENAI_API_KEY -- true", "ask"),
        ("envh --socket ctl.sock run --with OPENAI_API_KEY -- true", "ask"),
        ('envh --socket "$SOCK" run --with OPENAI_API_KEY -- true', "ask"),
        ("envh run --with OPENAI_API_KEY -- true; export ENVH_SOCKET=/tmp/strict.sock", None),
    ],
)
async def test_hook_stays_quiet_for_keys_that_need_no_approval(control: Path, harness: Harness, command: str, expected: str | None) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    output = await hook_against_broker(control, claude_payload(command))
    if expected is None:
        assert output is None, output
    else:
        assert output is not None and output["hookSpecificOutput"]["permissionDecision"] == expected


async def test_hook_asks_the_broker_the_command_names_and_cursor_gets_the_same_answer(control: Path, harness: Harness) -> None:
    make_auto(harness, "OPENAI_API_KEY")
    command = f"envh --socket {control} run --with OPENAI_API_KEY -- true"
    assert await hook_against_broker(Path(NO_BROKER), claude_payload(command)) is None
    assert await hook_against_broker(Path(NO_BROKER), claude_payload(f"ENVH_SOCKET=/tmp/strict.sock {command}")) is None
    assert await hook_against_broker(control, {"hook_event_name": "beforeShellExecution", "command": "envh run --with OPENAI_API_KEY -- true"}) is None
    assert harness.broker.state.requests == {}
