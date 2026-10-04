#!/usr/bin/env python3
"""envh hook for Claude Code (PreToolUse) and Cursor (beforeShellExecution). Standard library only.

Asks in the app exactly when a command will make the envh console ask for the vault passphrase, and refuses sudo
from the agent. It reads the command the way a shell does, so envh only counts where it runs: as a command name,
including after `&&`, `;`, a pipe or a newline, inside `$(...)`, backticks, `bash -c` or `eval`, behind wrappers like
`timeout` or `uv run`, or with a path prefix. envh inside an argument, a quoted string, a comment or a heredoc body is
a mention and never asks, unless the quoting cannot be read at all; then text that looks like such a request asks.
A script file, an interpreter one-liner or a variable-built command gets around it, and
nothing depends on it: a disguised session-less `envh run` still prompts on the envh console and a disguised
`envh session start` still needs the passphrase typed there. The hook is attention plus a second chance to deny, not
the boundary. sudo, su, doas and pkexec are refused wherever they appear in the command text.
With no opinion it prints nothing, for Cursor too: a Cursor "allow" can skip Cursor's own approval of a command.
Install: see settings.snippet.json and cursor/hooks.snippet.json.
"""

import json
import posixpath
import re
import shlex
import sys
from typing import Any

PRIVILEGE = re.compile(r"(?<![\w.-])(?:[\w./-]*/)?(?:sudo|su|doas|pkexec)(?![\w./-])")
PASSPHRASE_FORM = re.compile(r"(?<![\w.-])(?:[\w./-]*/)?envh\s+(?:session\s+start|preset\s+propose|import|run)\b")
SESSION_ASSIGNMENT = "ENVH_SESSION="
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
HEREDOC = re.compile(r"<<(-?)[ \t]*\\?(['\"]?)([\w.-]+)\2")
OPERATOR_CHARACTERS = ";&|()<>\n"
RESERVED_WORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until"}
WRAPPERS = {"command", "env", "exec", "nice", "nohup", "time", "timeout", "xargs"}
WRAPPER_OPTIONS_WITH_VALUE = {"-n", "-s", "-k", "-u", "-I", "-L", "-P", "-d"}
RUNNERS = {"uv", "poetry"}
SHELLS = {"bash", "sh", "zsh", "dash"}
MAX_NESTING = 3
HELP = {"-h", "--help"}
RUN_OPTIONS_WITH_VALUE = {"--session", "--preset", "--with", "--reason"}
SESSION_REASON = "envh: starting a secret session; approve it on the envh console with your vault passphrase"
PROPOSE_REASON = "envh: proposing a preset change; review the diff on the envh console"
IMPORT_REASON = "envh: importing secrets; this is an interactive wizard meant for a human"
RUN_REASON = "envh: running with secrets outside a session prompts on the envh console for every run; approve here first"
UNREADABLE_REASON = "envh: this command's quoting could not be read, and it may start an envh request that needs your passphrase"


def closing_parenthesis(script: str, start: int) -> int:
    depth = 1
    quote = ""
    index = start
    while index < len(script):
        character = script[index]
        if character == "\\" and quote != "'":
            index += 1
        elif quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(script)


def after_heredoc_bodies(script: str, start: int, heredocs: list[tuple[str, bool]]) -> int:
    """Where the script continues after the heredoc bodies that begin at start; `<<-` lets the terminator be indented
    with tabs, `<<` needs it alone on its line."""
    position = start
    for delimiter, strips_tabs in heredocs:
        while position < len(script):
            line_end = script.find("\n", position)
            line_end = len(script) if line_end == -1 else line_end
            line = script[position:line_end]
            position = line_end + 1
            if (line.lstrip("\t") if strips_tabs else line) == delimiter:
                break
    return min(position, len(script))


def split_substitutions(script: str) -> tuple[str, list[str]]:
    """The script's top level, without comments and heredoc bodies and with each `$(...)`, backtick or `<(...)`
    substitution replaced by a placeholder word, plus the scripts inside those substitutions."""
    top: list[str] = []
    inner: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    quote = ""
    index = 0
    while index < len(script):
        character = script[index]
        if quote == "'":
            quote = "" if character == "'" else quote
        elif script.startswith("\\\n", index):
            top.append(" ")
            index += 2
            continue
        elif character == "\\":
            top.append(script[index : index + 2])
            index += 2
            continue
        elif character == "'" and not quote:
            quote = "'"
        elif character == '"':
            quote = "" if quote else '"'
        elif script.startswith("$((", index):
            top.append("_")
            index = closing_parenthesis(script, index + 2) + 1
            continue
        elif script.startswith("$(", index) or (not quote and script.startswith(("<(", ">("), index)):
            end = closing_parenthesis(script, index + 2)
            inner.append(script[index + 2 : end])
            top.append("_")
            index = end + 1
            continue
        elif character == "`":
            end = script.find("`", index + 1)
            end = len(script) if end == -1 else end
            inner.append(script[index + 1 : end])
            top.append("_")
            index = end + 1
            continue
        elif quote:
            pass
        elif character == "#" and (index == 0 or script[index - 1] in " \t" + OPERATOR_CHARACTERS):
            end = script.find("\n", index)
            index = len(script) if end == -1 else end
            continue
        elif character == "\n" and heredocs:
            index = after_heredoc_bodies(script, index + 1, heredocs)
            heredocs = []
            top.append("\n")
            continue
        elif script.startswith("<<", index) and not script.startswith("<<<", index) and script[index - 1 : index] != "<":
            heredoc = HEREDOC.match(script, index)
            if heredoc:
                heredocs.append((heredoc.group(3), heredoc.group(1) == "-"))
                top.append(" ")
                index = heredoc.end()
                continue
        top.append(character)
        index += 1
    return "".join(top), inner


def split_on_operators(script: str) -> list[list[str]]:
    """The words of each simple command in a script without substitutions; raises ValueError when its quotes do not
    close."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=OPERATOR_CHARACTERS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    commands: list[list[str]] = [[]]
    skip_redirection_target = False
    for token in list(lexer):
        if skip_redirection_target:
            skip_redirection_target = False
        elif not token or any(character not in OPERATOR_CHARACTERS for character in token):
            commands[-1].append(token)
        elif "<" in token or ">" in token:
            skip_redirection_target = True
        else:
            commands.append([])
    return commands


def simple_commands(script: str, nesting: int = 0) -> list[list[str]]:
    """The words of every simple command the script runs, including those in substitutions, `bash -c` and `eval`;
    raises ValueError when quotes do not close."""
    top, substitutions = split_substitutions(script)
    commands = [words for inner in substitutions for words in simple_commands(inner, nesting)]
    for words in split_on_operators(top):
        if not words:
            continue
        commands.append(words)
        nested = nested_script(command_words(words)) if nesting < MAX_NESTING else None
        if nested is not None:
            commands += simple_commands(nested, nesting + 1)
    return commands


def command_words(words: list[str]) -> list[str]:
    """The words from the command name on, past leading assignments, reserved words and wrappers like timeout."""
    index = 0
    while index < len(words):
        name = posixpath.basename(words[index])
        if ASSIGNMENT.match(words[index]) or words[index] in RESERVED_WORDS:
            index += 1
            continue
        if name in WRAPPERS:
            index += 1
        elif name in RUNNERS and words[index + 1 : index + 2] == ["run"]:
            index += 2
        else:
            break
        while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.match(words[index])):
            index += 2 if words[index] in WRAPPER_OPTIONS_WITH_VALUE else 1
        if name == "timeout":
            index += 1
    return words[index:]


def nested_script(words: list[str]) -> str | None:
    """The script that `bash -c` or `eval` runs, or None for any other command."""
    if not words:
        return None
    name = posixpath.basename(words[0])
    if name == "eval":
        return " ".join(words[1:])
    if name not in SHELLS:
        return None
    for index, word in enumerate(words[1:], 1):
        if not word.startswith("-"):
            return None
        if not word.startswith("--") and "c" in word:
            return words[index + 1] if index + 1 < len(words) else None
    return None


def run_options(arguments: list[str]) -> list[str]:
    """The options that belong to `envh run` itself: the ones before `--` or the command it runs."""
    options: list[str] = []
    index = 0
    while index < len(arguments) and arguments[index] != "--" and arguments[index].startswith("-"):
        options.append(arguments[index])
        index += 2 if arguments[index] in RUN_OPTIONS_WITH_VALUE else 1
    return options


def passphrase_reason(arguments: list[str], session_assigned: bool) -> str | None:
    """Why the envh console will ask for the vault passphrase for this invocation, or None when it will not."""
    while arguments[:1] and arguments[0].startswith("--socket"):
        arguments = arguments[1:] if "=" in arguments[0] else arguments[2:]
    match arguments:
        case ["session", "start", *rest] if not HELP & set(rest):
            return SESSION_REASON
        case ["preset", "propose", *rest] if not HELP & set(rest):
            return PROPOSE_REASON
        case ["import", *rest] if not ({"--dry-run"} | HELP) & set(rest):
            return IMPORT_REASON
        case ["run", *rest]:
            options = run_options(rest)
            in_session = session_assigned or any(option == "--session" or option.startswith("--session=") for option in options)
            return None if in_session or HELP & set(options) else RUN_REASON
    return None


def decide(command: str) -> tuple[str, str] | None:
    if PRIVILEGE.search(command):
        return "deny", "envh policy: agents never run sudo, su, doas or pkexec; a cached credential could be reused. Ask the human to run it."
    try:
        commands = simple_commands(command)
    except ValueError:
        return ("ask", UNREADABLE_REASON) if PASSPHRASE_FORM.search(command) else None
    session_set = False
    for words in commands:
        name_and_arguments = command_words(words)
        if not name_and_arguments or name_and_arguments[0] == "export":
            session_set = session_set or any(word.startswith(SESSION_ASSIGNMENT) for word in words)
        elif posixpath.basename(name_and_arguments[0]) == "envh":
            prefix = words[: len(words) - len(name_and_arguments)]
            in_session = session_set or any(word.startswith(SESSION_ASSIGNMENT) for word in prefix)
            reason = passphrase_reason(name_and_arguments[1:], in_session)
            if reason is not None:
                return "ask", reason
    return None


def response(payload: dict[str, Any]) -> dict[str, Any] | None:
    from_cursor = payload.get("hook_event_name") == "beforeShellExecution"
    if from_cursor:
        command = payload.get("command")
    elif payload.get("tool_name") == "Bash":
        command = (payload.get("tool_input") or {}).get("command")
    else:
        return None
    decision = decide(str(command or ""))
    if decision is None:
        return None
    verdict, reason = decision
    if from_cursor:
        return {"permission": verdict, "user_message": reason, "agent_message": reason}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": verdict, "permissionDecisionReason": reason}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    output = response(payload)
    if output is not None:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
