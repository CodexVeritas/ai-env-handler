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
        # the --session must belong to this invocation, not to the command after -- or to a later segment
        ("envh run --with A -- python x.py --session abc", "ask"),
        ("envh run --session abc -- python x.py --other", None),
        ("envh run --with A -- true; echo --session", "ask"),
        ('envh run --with A --reason "no --session here" -- true', "ask"),
        ("envh run --session-file x --with A -- true", "ask"),
        ("envh run --session=abc -- true", None),
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


@pytest.mark.parametrize(
    "command",
    [
        # at the start of a command, after any operator or keyword
        "sudo apt install x",
        "su - root",
        "doas ls",
        "pkexec cat /etc/shadow",
        "ls && sudo -n true",
        "envh list && sudo -k",
        "ls || sudo ls",
        "ls; sudo ls",
        "ls | sudo tee /etc/x",
        "ls |& sudo tee /etc/x",
        "sleep 1 & sudo ls",
        "ls\nsudo ls",
        "true && \\\n  sudo ls",
        "(sudo ls)",
        "if sudo -n true; then ls; fi",
        "if true; then ls; else sudo ls; fi",
        "while true; do sudo ls; done",
        "! sudo ls",
        "{ sudo ls; }",
        "f() { sudo ls; }; f",
        "function f { sudo ls; }; f",
        "case x in x) sudo ls;; esac",
        # however the name is spelled
        "/usr/bin/sudo -n ls",
        "./sudo ls",
        '"sudo" ls',
        "s\\udo ls",
        'su""do ls',
        "\\sudo ls",
        "$'sudo' ls",
        "su\\\ndo ls",
        # behind assignments, redirections and wrappers
        "TERM=dumb sudo -n ls",
        "A=1 B=2 sudo ls",
        ">out sudo ls",
        "2>/dev/null sudo ls",
        "env sudo ls",
        "env -i PATH=/usr/bin sudo ls",
        "env -u HOME sudo ls",
        "nice -n 10 sudo ls",
        "nohup sudo ls &",
        "xargs sudo rm",
        "xargs -0 -n1 sudo rm",
        "xargs -I {} sudo cp {} /etc/",
        "exec sudo ls",
        "time -p sudo ls",
        "command sudo ls",
        "timeout -s KILL 5s sudo ls",
        "stdbuf -oL sudo journalctl -f",
        "setsid sudo ls",
        "nohup env FOO=1 nice sudo ls",
        "find . -exec sudo rm {} \\;",
        # inside text a shell runs
        "echo $(sudo ls)",
        "echo `sudo ls`",
        'echo "$(sudo ls)"',
        'git commit -m "run `sudo ls`"',
        "x=$(sudo ls)",
        "arr=(a $(sudo ls) b)",
        "diff <(sudo cat /etc/shadow) /dev/null",
        "cat <<EOF\n$(sudo ls)\nEOF",
        "cat <<EOF\nrun `sudo ls`\nEOF",
        "bash -c 'sudo -n ls'",
        'sh -c "cd /tmp && sudo ls"',
        "bash -lc 'sudo ls'",
        "eval 'sudo ls'",
        "eval sudo ls",
        "ssh host sudo reboot",
        "ssh -p 22 host 'sudo reboot'",
        "ssh host <<'EOF'\nsudo reboot\nEOF",
        "bash <<'EOF'\nsudo ls\nEOF",
        "bash <<< 'sudo ls'",
        "xargs sh -c 'sudo ls'",
        "find . -name x -execdir sh -c 'sudo rm \"$1\"' _ {} \\;",
        # after a comment or a heredoc ends
        "# what's next\nsudo ls",
        "cat <<'EOF'\nsudo in a body\nEOF\nsudo ls",
        "cat <<-EOF\n\tsudo in a body\n\tEOF\nsudo ls",
        "x=$(cat <<EOF\nbody\nEOF); sudo ls",
    ],
)
def test_hook_denies_privilege_escalation_where_the_shell_would_run_it(command: str) -> None:
    output = run_hook(command)
    assert output is not None, command
    assert output["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        # arguments, quoted or not
        'grep -n "sudo" README.md',
        "grep -n sudo README.md",
        "grep -rn 'sudo\\|doas' .",
        'echo "use sudo to install"',
        "printf '%s\\n' \"su - root\"",
        "echo 'a; sudo ls'",
        "echo hi > sudo",
        "./sudo.sh",
        "which sudo",
        "command -v sudo",
        "for name in sudo su doas pkexec; do echo $name; done",
        "words=(sudo su doas pkexec); echo ${words[@]}",
        "case $x in sudo) echo hi;; esac",
        "time grep -rn sudo .",
        'git ls-files | xargs grep -n "sudo"',
        "find . -exec grep -l sudo {} +",
        "sed -i 's/sudo/doas/g' README.md",
        "ls # sudo would be wrong here",
        # heredocs and commit messages
        "python3 - <<'EOF'\ntext = text.replace(\"sudo\", \"doas\")\nEOF",
        "cat <<EOF\nNever run sudo here\nsudo ls\nEOF",
        "cat <<'EOF' > notes.md\nRun `sudo envh-console`\nEOF",
        'git commit -m "Deny sudo only in command position"',
        "git commit -m \"$(cat <<'EOF'\nFix: sudo; su && pkexec | doas\n\n(sudo) `sudo`\nEOF\n)\" && git push",
        'gh pr create --title "Hook: allow sudo as data" --body "Stops blocking \\`grep sudo\\`."',
        # strings a shell runs, where the word is still data
        "bash -c 'grep -n sudo README.md'",
        'bash -c "echo \\"sudo\\""',
        "ssh host 'grep sudo /etc/group'",
        "python3 -c 'print(\"sudo\")'",
    ],
)
def test_hook_allows_privilege_words_as_data(command: str) -> None:
    assert run_hook(command) is None


def test_hook_falls_back_to_the_word_match_when_nested_too_deeply_to_parse() -> None:
    nested = "echo " + "$(" * 3000 + "{} ls" + ")" * 3000
    assert run_hook(nested.format("true")) is None
    output = run_hook(nested.format("sudo"))
    assert output is not None
    assert output["permissionDecision"] == "deny"


def test_hook_ignores_other_tools() -> None:
    assert run_hook("sudo rm -rf /", tool="Read") is None
