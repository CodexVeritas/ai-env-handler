import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "claude" / "hooks" / "envh_ask.py"
CURSOR_MATCHER = json.loads((ROOT / "cursor" / "hooks.snippet.json").read_text())["hooks"]["beforeShellExecution"][0]["matcher"]


def run_hook_raw(payload: dict) -> dict | None:
    result = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def run_hook(command: str, tool: str = "Bash") -> dict | None:
    output = run_hook_raw({"tool_name": tool, "tool_input": {"command": command}, "hook_event_name": "PreToolUse"})
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
        ("envh --socket /tmp/envh-dev/ctl.sock session start p --minutes 5", "ask"),
        ("envh --socket=/tmp/envh-dev/ctl.sock run --with A -- true", "ask"),
        ("envh run --session abc --reason y -- python x.py", None),
        ("ENVH_SESSION=abc envh run -- python x.py", None),
        ("env ENVH_SESSION=abc envh run -- python x.py", None),
        ("export ENVH_SESSION=abc && envh run -- python x.py", None),
        ("export ENVH_SESSION=abc; echo $(envh run -- python x.py)", None),
        ("ENVH_SESSION=abc; envh run -- python x.py", None),
        ("grep ENVH_SESSION= README.md && envh run --with A -- true", "ask"),
        ("envh run --with A -- true  # ENVH_SESSION= later", "ask"),
        ("envh run --with A -- env ENVH_SESSION=abc true", "ask"),
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
        assert re.search(CURSOR_MATCHER, command), f"the Cursor matcher would skip {command!r}"


def test_hook_falls_back_to_the_word_match_when_nested_too_deeply_to_parse() -> None:
    nested = "echo " + "$(" * 3000 + "{} ls" + ")" * 3000
    assert run_hook(nested.format("true")) is None
    output = run_hook(nested.format("envh session start p --minutes 5"))
    assert output is not None
    assert output["permissionDecision"] == "ask"


def test_hook_ignores_other_tools() -> None:
    assert run_hook("envh session start p --minutes 5", tool="Read") is None
