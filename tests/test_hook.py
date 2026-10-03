import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "claude" / "hooks" / "envh_ask.py"


def run_hook(command: str, tool: str = "Bash") -> dict | None:
    payload = {"tool_name": tool, "tool_input": {"command": command}, "hook_event_name": "PreToolUse"}
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # plain forms
        ("envh session start research --minutes 60 --reason x", "ask"),
        ("envh preset propose draft.yaml", "ask"),
        ("envh import ~/code", "ask"),
        ("envh run --with OPENAI_API_KEY -- python x.py", "ask"),
        ("envh run --session abc --reason y -- python x.py", None),
        ("ENVH_SESSION=abc envh run -- python x.py", None),
        ("PYTHONPATH=. envh run --with KEY -- python x.py", "ask"),
        ("envh list", None),
        ("envh status && git status", None),
        ("envh session wait 7", None),
        ("envh session end abc", None),
        ("envh preset validate d.yaml", None),
        ("envh --help", None),
        # chaining and shell constructs
        ("cd repo && envh session start --with A --minutes 5", "ask"),
        ("true&&envh run --with A -- true", "ask"),
        ("false || envh session start p --minutes 5", "ask"),
        ("git pull; envh run -- python x.py", "ask"),
        ("(envh session start p --minutes 5)", "ask"),
        ("id=$(envh session start p --minutes 5 --quiet)", "ask"),
        ("id=`envh session start p --minutes 5 --quiet`", "ask"),
        ("envh run --session abc -- python x.py && envh run --with B -- true", "ask"),
        ("envh run --with B -- true | tee log; envh list", "ask"),
        # disguises
        ("/usr/local/bin/envh run --with A -- true", "ask"),
        ("./envh session start p --minutes 5", "ask"),
        ("bash -c 'envh session start p --minutes 5'", "ask"),
        ('sh -c "envh run --with A -- true"', "ask"),
        ("python -c \"import subprocess; subprocess.run(['envh','session','start','p','--minutes','5'])\"", "ask"),
        ("timeout 600 envh session start p --minutes 5", "ask"),
        ("nohup envh run --with A -- true &", "ask"),
        ("echo envh", "ask"),
        ("uv run envh run --session abc -- python x.py", None),
        # the --session must belong to this invocation
        ("envh run --with A -- python x.py --session abc", None),
        ("envh run --with A -- true; echo --session", "ask"),
        # privilege escalation is denied however it is spelled
        ("sudo apt install x", "deny"),
        ("ls && sudo -n true", "deny"),
        ("TERM=dumb sudo -n ls", "deny"),
        ("/usr/bin/sudo -n ls", "deny"),
        ("bash -c 'sudo -n ls'", "deny"),
        ("su - root", "deny"),
        ("pkexec cat /etc/shadow", "deny"),
        ("envh list && sudo -k", "deny"),
        # innocents
        ("echo sudoku", None),
        ("cat docs/su/notes.md", None),
        ("ls /opt/envh/bin", None),
        ("git status", None),
        ("python -c 'print(1)'", None),
    ],
)
def test_hook_decisions(command: str, expected: str | None) -> None:
    output = run_hook(command)
    if expected is None:
        assert output is None, output
    else:
        assert output is not None, command
        assert output["permissionDecision"] == expected
        assert output["hookEventName"] == "PreToolUse"


def test_hook_ignores_other_tools() -> None:
    assert run_hook("sudo rm -rf /", tool="Read") is None
