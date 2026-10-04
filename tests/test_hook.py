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
        # privilege escalation is denied where the shell would run it: at the start of a command
        ("sudo apt install x", "deny"),
        ("su - root", "deny"),
        ("doas ls", "deny"),
        ("pkexec cat /etc/shadow", "deny"),
        ("ls && sudo -n true", "deny"),
        ("envh list && sudo -k", "deny"),
        ("ls || sudo ls", "deny"),
        ("ls; sudo ls", "deny"),
        ("ls | sudo tee /etc/x", "deny"),
        ("ls |& sudo tee /etc/x", "deny"),
        ("sleep 1 & sudo ls", "deny"),
        ("ls\nsudo ls", "deny"),
        ("true && \\\n  sudo ls", "deny"),
        ("(sudo ls)", "deny"),
        ("if sudo -n true; then ls; fi", "deny"),
        ("if true; then ls; else sudo ls; fi", "deny"),
        ("while true; do sudo ls; done", "deny"),
        ("! sudo ls", "deny"),
        ("{ sudo ls; }", "deny"),
        ("f() { sudo ls; }; f", "deny"),
        ("function f { sudo ls; }; f", "deny"),
        ("case x in x) sudo ls;; esac", "deny"),
        # however the name is spelled
        ("/usr/bin/sudo -n ls", "deny"),
        ("./sudo ls", "deny"),
        ('"sudo" ls', "deny"),
        ('su""do ls', "deny"),
        ("\\sudo ls", "deny"),
        ("$'sudo' ls", "deny"),
        ("su\\\ndo ls", "deny"),
        # behind assignments, redirections and wrappers
        ("TERM=dumb sudo -n ls", "deny"),
        ("A=1 B=2 sudo ls", "deny"),
        (">out sudo ls", "deny"),
        ("2>/dev/null sudo ls", "deny"),
        ("env sudo ls", "deny"),
        ("env -i PATH=/usr/bin sudo ls", "deny"),
        ("env -u HOME sudo ls", "deny"),
        ("nice -n 10 sudo ls", "deny"),
        ("nohup sudo ls &", "deny"),
        ("xargs sudo rm", "deny"),
        ("xargs -0 -n1 sudo rm", "deny"),
        ("xargs -I {} sudo cp {} /etc/", "deny"),
        ("exec sudo ls", "deny"),
        ("time -p sudo ls", "deny"),
        ("command sudo ls", "deny"),
        ("timeout -s KILL 5s sudo ls", "deny"),
        ("stdbuf -oL sudo journalctl -f", "deny"),
        ("setsid sudo ls", "deny"),
        ("nohup env FOO=1 nice sudo ls", "deny"),
        ("find . -exec sudo rm {} \\;", "deny"),
        ("uv run sudo ls", "deny"),
        # inside text a shell runs
        ("echo $(sudo ls)", "deny"),
        ("echo `sudo ls`", "deny"),
        ('echo "$(sudo ls)"', "deny"),
        ('git commit -m "run `sudo ls`"', "deny"),
        ("x=$(sudo ls)", "deny"),
        ("arr=(a $(sudo ls) b)", "deny"),
        ("diff <(sudo cat /etc/shadow) /dev/null", "deny"),
        ("cat <<EOF\n$(sudo ls)\nEOF", "deny"),
        ("cat <<EOF\nrun `sudo ls`\nEOF", "deny"),
        ("bash -c 'sudo -n ls'", "deny"),
        ('sh -c "cd /tmp && sudo ls"', "deny"),
        ("bash -lc 'sudo ls'", "deny"),
        ("eval 'sudo ls'", "deny"),
        ("eval sudo ls", "deny"),
        ("ssh host sudo reboot", "deny"),
        ("ssh -p 22 host 'sudo reboot'", "deny"),
        ("ssh host <<'EOF'\nsudo reboot\nEOF", "deny"),
        ("bash <<'EOF'\nsudo ls\nEOF", "deny"),
        ("bash <<< 'sudo ls'", "deny"),
        ("xargs sh -c 'sudo ls'", "deny"),
        ("find . -name x -execdir sh -c 'sudo rm \"$1\"' _ {} \\;", "deny"),
        # after a comment or a heredoc ends
        ("# what's next\nsudo ls", "deny"),
        ("cat <<'EOF'\nsudo in a body\nEOF\nsudo ls", "deny"),
        ("cat <<-EOF\n\tsudo in a body\n\tEOF\nsudo ls", "deny"),
        ("x=$(cat <<EOF\nbody\nEOF); sudo ls", "deny"),
        ("echo $((1<<2))\nsudo ls", "deny"),
        # a privileged word as data passes
        ('grep -n "sudo" README.md', None),
        ("grep -n sudo README.md", None),
        ("grep -rn 'sudo\\|doas' .", None),
        ('echo "use sudo to install"', None),
        ("printf '%s\\n' \"su - root\"", None),
        ("echo 'a; sudo ls'", None),
        ("echo hi > sudo", None),
        ("./sudo.sh", None),
        ("which sudo", None),
        ("command -v sudo", None),
        ("for name in sudo su doas pkexec; do echo $name; done", None),
        ("words=(sudo su doas pkexec); echo ${words[@]}", None),
        ("case $x in sudo) echo hi;; esac", None),
        ("time grep -rn sudo .", None),
        ('git ls-files | xargs grep -n "sudo"', None),
        ("find . -exec grep -l sudo {} +", None),
        ("sed -i 's/sudo/doas/g' README.md", None),
        ("ls # sudo would be wrong here", None),
        ("python3 - <<'EOF'\ntext = text.replace(\"sudo\", \"doas\")\nEOF", None),
        ("cat <<EOF\nNever run sudo here\nsudo ls\nEOF", None),
        ("cat <<'EOF' > notes.md\nRun `sudo envh-console`\nEOF", None),
        ('git commit -m "Deny sudo only in command position"', None),
        ("git commit -m \"$(cat <<'EOF'\nDon't run sudo here\nEOF\n)\"", None),
        ("git commit -m \"$(cat <<'EOF'\nFix: sudo; su && pkexec | doas\n\n(sudo) `sudo`\nEOF\n)\" && git push", None),
        ('gh pr create --title "Hook: allow sudo as data" --body "Stops blocking \\`grep sudo\\`."', None),
        ("bash -c 'grep -n sudo README.md'", None),
        ('bash -c "echo \\"sudo\\""', None),
        ("ssh host 'grep sudo /etc/group'", None),
        ("python3 -c 'print(\"sudo\")'", None),
        # innocents
        ("echo sudoku", None),
        ("cat docs/su/notes.md", None),
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
    output = run_hook(nested.format("sudo"))
    assert output is not None
    assert output["permissionDecision"] == "deny"


def test_hook_ignores_other_tools() -> None:
    assert run_hook("sudo rm -rf /", tool="Read") is None
