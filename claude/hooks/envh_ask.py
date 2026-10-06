#!/usr/bin/env python3
"""envh hook for Claude Code (PreToolUse) and Cursor (beforeShellExecution). Standard library only.

Asks in the app exactly when a command will make the envh console ask for the vault passphrase. It reads the command
the way a shell does, so envh only counts where it runs: as a command name, including after `&&`, `;`, a pipe or a
newline, inside `$(...)`, backticks, `bash -c` or `eval`, behind wrappers like `timeout` or `uv run`, or with a path
prefix. envh inside an argument, a quoted string, a comment or a heredoc body is a mention and never asks, though a
`$(...)` or backticks that bash expands there still count. If a quote never closes, text that looks like such a
request asks.
For `envh run` and `envh session start` it first asks the broker whether those keys need approval at all. Keys marked
auto don't, so it stays quiet for them. It asks anyway when it can't be sure what it asked about: the broker can't
say, bash rewrites an option first (a `$(...)`, backticks or a `$`), or the script sets ENVH_SOCKET.
A script file, an interpreter one-liner, a pipe into a shell or a variable-built command gets around it, and
nothing depends on it: a disguised session-less `envh run` still prompts on the envh console and a disguised
`envh session start` still needs the passphrase typed there. The hook is attention plus a second chance to deny, not
the boundary.
With no opinion it prints nothing, for Cursor too: a Cursor "allow" can skip Cursor's own approval of a command.
Install: see settings.snippet.json and cursor/hooks.snippet.json.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import socket
import sys
from dataclasses import dataclass, field
from typing import Any

PASSPHRASE_FORM = re.compile(r"(?<![\w.-])(?:[\w./-]*/)?envh\s+(?:session\s+start|preset\s+propose|import|run)\b")
SESSION_ASSIGNMENT = "ENVH_SESSION="
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
RESERVED_WORDS = {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "coproc", "function"}
# each wrapper's options that take a value
WRAPPERS = {
    "command": set(),
    "env": {"-u", "--unset", "-C", "--chdir"},
    "exec": {"-a"},
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "setsid": set(),
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
    "time": {"-f", "--format", "-o", "--output"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "xargs": {"-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars"},
}
RUNNERS = {"uv", "poetry"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
MAX_NESTING = 3
HELP = {"-h", "--help"}
RUN_OPTIONS_WITH_VALUE = {"--session", "--preset", "--with", "--reason"}
SESSION_REASON = "envh: starting a secret session; approve it on the envh console with your vault passphrase"
PROPOSE_REASON = "envh: proposing a preset change; review the diff on the envh console"
IMPORT_REASON = "envh: importing secrets; this is an interactive wizard meant for a human"
RUN_REASON = "envh: running one command with secrets; approve it on the envh console with your vault passphrase"
UNREADABLE_REASON = "envh: this command's quoting could not be read, and it may start an envh request that needs your passphrase"
KIND_BY_REASON = {RUN_REASON: "run", SESSION_REASON: "session"}
SOCKET_ENV_VAR = "ENVH_SOCKET"
DEFAULT_SOCKET = "/run/envh/ctl.sock"
BROKER_TIMEOUT_SECONDS = 2
# per request kind: the options that take a value, with the list the value goes in, and the flags
VALUE_OPTIONS: dict[str, dict[str, str | None]] = {
    "run": {"--preset": "presets", "--with": "with", "--reason": None},
    "session": {"--with": "with", "--minutes": None, "--reason": None},
}
FLAG_OPTIONS = {"run": {"--keep-background"}, "session": {"--quiet"}}

OPERATORS = ("<<<", "<<-", "&>>", ";;&", "&&", "||", ";;", ";&", "|&", ">>", ">|", ">&", "<&", "<>", "<<", "&>", ";", "&", "|", "<", ">", "(", ")", "\n")
SEPARATORS = {";", "&", "&&", "||", "|", "|&", ";;", ";&", ";;&", "(", ")", "\n"}
METACHARACTERS = set(" \t\n;&|<>()")
REDIRECT_FD = re.compile(r"\d+|\{\w+\}")


def run_options(arguments: list[str]) -> list[str]:
    """The options that belong to `envh run` itself: the ones before `--` or the command it runs."""
    options: list[str] = []
    index = 0
    while index < len(arguments) and arguments[index] != "--" and arguments[index].startswith("-"):
        options.append(arguments[index])
        index += 2 if arguments[index] in RUN_OPTIONS_WITH_VALUE else 1
    return options


def socket_and_arguments(arguments: list[str], script_sets_socket: bool) -> tuple[str | None, list[str]]:
    """The broker socket an envh invocation talks to, and its arguments after --socket. The socket is None when the hook
    can't tell which broker that is: the script sets ENVH_SOCKET, or the path is relative or rewritten by bash."""
    socket_path = None if script_sets_socket else os.environ.get(SOCKET_ENV_VAR) or DEFAULT_SOCKET
    while arguments[:1] and arguments[0].startswith("--socket"):
        if "=" in arguments[0]:
            word, value = arguments[0], arguments[0].partition("=")[2]
            arguments = arguments[1:]
        else:
            word = value = (arguments[1:2] or [""])[0]
            arguments = arguments[2:]
        socket_path = None if isinstance(word, Expanded) else value
    return (socket_path if socket_path and posixpath.isabs(socket_path) else None), arguments


def passphrase_reason(arguments: list[str], session_assigned: bool) -> str | None:
    """Why the envh console will ask for the vault passphrase for this invocation, or None when it will not."""
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


def key_request(kind: str, arguments: list[str]) -> dict[str, Any] | None:
    """The presets and variables an `envh run` or `envh session start` asks for, as the client sends them; None when an
    argument isn't one the hook reads exactly, such as an abbreviated option or one bash rewrites."""
    request: dict[str, Any] = {"op": "needs_approval", "kind": kind, "presets": [], "with": []}
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if isinstance(word, Expanded):
            return None
        option, has_value, value = word.partition("=")
        if kind == "run" and (word == "--" or not word.startswith("-")):
            break
        if kind == "session" and not word.startswith("-"):
            request["presets"].append(word)
        elif option in VALUE_OPTIONS[kind]:
            if not has_value:
                index += 1
                if index == len(arguments) or isinstance(arguments[index], Expanded):
                    return None
                value = arguments[index]
            target = VALUE_OPTIONS[kind][option]
            if target is not None:
                request[target].append(value)
        elif word not in FLAG_OPTIONS[kind]:
            return None
        index += 1
    for target in ("presets", "with"):
        request[target] = [part.strip() for value in request[target] for part in value.split(",") if part.strip()]
    return request


def broker_needs_approval(request: dict[str, Any], socket_path: str) -> bool:
    """What the broker says about the request; True whenever it can't say, such as when it isn't running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(BROKER_TIMEOUT_SECONDS)
            connection.connect(socket_path)
            connection.sendall(json.dumps(request).encode() + b"\n")
            with connection.makefile("rb") as replies:
                reply = json.loads(replies.readline())
    except (OSError, ValueError):
        return True
    return not (isinstance(reply, dict) and reply.get("ok") is True and reply.get("needs_approval") is False)


def granted_at_once(reason: str, arguments: list[str], socket_path: str | None) -> bool:
    """True when the broker grants this run or session start without the console, since none of its keys needs approval."""
    kind = KIND_BY_REASON.get(reason)
    request = key_request(kind, arguments[2 if kind == "session" else 1 :]) if kind else None
    return request is not None and socket_path is not None and not broker_needs_approval(request, socket_path)


class Expanded(str):
    """A word bash rewrites before the command gets it, since it holds a $(...), backticks or a $. It reads as the text
    the lexer kept, so the parsing treats it like any other word; only what needs a word's exact value checks for it."""


@dataclass
class Word:
    value: str = ""
    quoted: bool = False
    substitutions: list[list[Word | str]] = field(default_factory=list)
    heredoc: str | None = None


@dataclass
class Command:
    words: list[str] = field(default_factory=list)
    stdin: list[str] = field(default_factory=list)
    substitutions: list[list[Word | str]] = field(default_factory=list)


class Lexer:
    """Splits shell text into words and operators the way bash does, closely enough to tell a command from data.
    A quote that never closes runs to the end of the text and sets `unclosed`."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.heredocs: list[tuple[Word, bool]] = []
        self.unclosed = False

    def peek(self, length: int = 1) -> str:
        return self.text[self.pos : self.pos + length]

    def tokens(self, until_paren: bool = False) -> list[Word | str]:
        """Words and operators up to the end of the text, or with `until_paren` up to the `)` that closes a $(...) or an
        array. As in bash, a heredoc opened in a $(...) never reads past its `)`, so the shift in `$((1<<2))` cannot
        swallow the next line."""
        tokens: list[Word | str] = []
        depth = 0
        heredocs_before = len(self.heredocs)
        while self.pos < len(self.text):
            operator = next((candidate for candidate in OPERATORS if self.text.startswith(candidate, self.pos)), "")
            if self.peek() in (" ", "\t") or self.peek(2) == "\\\n":
                self.pos += 2 if self.peek() == "\\" else 1
            elif self.peek() == "#":
                newline = self.text.find("\n", self.pos)
                self.pos = len(self.text) if newline == -1 else newline
            elif operator:
                self.pos += len(operator)
                if operator == ")" and depth == 0 and until_paren:
                    del self.heredocs[heredocs_before:]
                    return tokens
                depth += {"(": 1, ")": -1}.get(operator, 0)
                tokens.append(operator)
                if operator in ("<<", "<<-"):
                    self.heredoc_delimiter(tokens, strip_tabs=operator == "<<-")
                elif operator == "\n":
                    self.read_heredocs(in_substitution=until_paren)
            else:
                word = self.word()
                if word.quoted or not REDIRECT_FD.fullmatch(word.value) or self.peek() not in ("<", ">"):
                    tokens.append(word)
        return tokens

    def word(self) -> Word:
        word = Word()
        value = ""
        while self.pos < len(self.text) and self.peek() not in METACHARACTERS:
            if self.peek(2) == "\\\n":
                self.pos += 2
            elif self.peek() == "\\":
                value += self.peek(2)[1:]
                word.quoted = True
                self.pos += 2
            elif self.peek() == "'":
                end = self.text.find("'", self.pos + 1)
                self.unclosed = self.unclosed or end == -1
                end = len(self.text) if end == -1 else end
                value += self.text[self.pos + 1 : end]
                word.quoted = True
                self.pos = end + 1
            elif self.peek(2) == "$'":
                self.pos += 2
                value += self.ansi_c_quoted()
                word.quoted = True
            elif self.peek() == '"' or self.peek(2) == '$"':
                self.pos += 2 if self.peek() == "$" else 1
                value += self.expansions(word.substitutions, closer='"')
                word.quoted = True
            elif self.peek(2) == "$(":
                self.pos += 2
                word.substitutions.append(self.tokens(until_paren=True))
            elif self.peek() == "`":
                word.substitutions.append(self.backticks())
            else:
                value += self.peek()
                self.pos += 1
        word.value = value
        if self.peek() == "(" and not word.quoted and ASSIGNMENT.fullmatch(value):
            self.pos += 1
            array = self.tokens(until_paren=True)
            word.substitutions += [inner for element in array if isinstance(element, Word) for inner in element.substitutions]
        return word

    def expansions(self, substitutions: list[list[Word | str]], closer: str) -> str:
        """Literal text under double-quote rules, where only backslash, $(...) and backticks are special, up to `closer` or the end."""
        value = ""
        while self.pos < len(self.text) and self.peek() != closer:
            if self.peek() == "\\":
                value += "" if self.peek(2) == "\\\n" else self.peek(2)[1:]
                self.pos += 2
            elif self.peek(2) == "$(":
                self.pos += 2
                substitutions.append(self.tokens(until_paren=True))
            elif self.peek() == "`":
                substitutions.append(self.backticks())
            else:
                value += self.peek()
                self.pos += 1
        self.unclosed = self.unclosed or (closer != "" and self.pos >= len(self.text))
        self.pos += 1
        return value

    def backticks(self) -> list[Word | str]:
        self.pos += 1
        content = ""
        while self.pos < len(self.text) and self.peek() != "`":
            if self.peek() == "\\" and self.peek(2)[1:] in ("`", "\\", "$"):
                self.pos += 1
            content += self.peek()
            self.pos += 1
        self.pos += 1
        inner = Lexer(content)
        tokens = inner.tokens()
        self.unclosed = self.unclosed or inner.unclosed
        return tokens

    def ansi_c_quoted(self) -> str:
        value = ""
        while self.pos < len(self.text) and self.peek() != "'":
            if self.peek() == "\\":
                self.pos += 1
            value += self.peek()
            self.pos += 1
        self.unclosed = self.unclosed or self.pos >= len(self.text)
        self.pos += 1
        return value

    def heredoc_delimiter(self, tokens: list[Word | str], strip_tabs: bool) -> None:
        while self.peek() in (" ", "\t"):
            self.pos += 1
        if self.peek() and self.peek() not in METACHARACTERS:
            delimiter = self.word()
            tokens.append(delimiter)
            self.heredocs.append((delimiter, strip_tabs))

    def read_heredocs(self, in_substitution: bool) -> None:
        """Reads the bodies of the heredocs opened on the line that just ended. Inside $(...), bash also ends one at `DELIM)`."""
        for delimiter, strip_tabs in self.heredocs:
            lines: list[str] = []
            while self.pos < len(self.text):
                end = self.text.find("\n", self.pos)
                end = len(self.text) if end == -1 else end
                line = self.text[self.pos : end].lstrip("\t") if strip_tabs else self.text[self.pos : end]
                if in_substitution and line.startswith(delimiter.value + ")"):
                    self.pos = end - len(line) + len(delimiter.value)
                    break
                self.pos = end + 1
                if line == delimiter.value:
                    break
                lines.append(line)
            delimiter.heredoc = "\n".join(lines)
            if not delimiter.quoted:
                body = Lexer(delimiter.heredoc)
                body.expansions(delimiter.substitutions, closer="")
                self.unclosed = self.unclosed or body.unclosed
        self.heredocs = []


def split_commands(tokens: list[Word | str]) -> list[Command]:
    """Splits tokens at shell operators. A here-string or heredoc becomes the command's stdin; other redirection targets are dropped."""
    commands = [Command()]
    redirect = ""
    for token in tokens:
        command = commands[-1]
        if isinstance(token, str):
            if token in SEPARATORS:
                commands.append(Command())
            redirect = "" if token in SEPARATORS else token
            continue
        command.substitutions += token.substitutions
        if redirect == "<<<":
            command.stdin.append(token.value)
        elif token.heredoc is not None:
            command.stdin.append(token.heredoc)
        elif not redirect:
            command.words.append(Expanded(token.value) if token.substitutions or "$" in token.value else token.value)
        redirect = ""
    return commands


def command_words(words: list[str]) -> list[str]:
    """The words from the command name on, past assignments, reserved words and wrappers like timeout or uv run; none
    for `command -v`, which only looks the name up."""
    index = 0
    while index < len(words):
        name = posixpath.basename(words[index])
        following = words[index + 1 : index + 2]
        if ASSIGNMENT.match(words[index]) or words[index] in RESERVED_WORDS:
            index += 2 if words[index] == "function" else 1
            continue
        if name == "command" and following in (["-v"], ["-V"]):
            return []
        if name in WRAPPERS:
            index += 1
        elif name in RUNNERS and following == ["run"]:
            index += 2
        else:
            break
        while index < len(words) and words[index].startswith("-"):
            option = words[index]
            index += 2 if option in WRAPPERS.get(name, set()) else 1
            if option == "--":
                break
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


def simple_commands(script: str, nesting: int = 0) -> list[list[str]]:
    """The words of every simple command the script runs, in order: a command's substitutions before it, and after it
    what `bash -c` or `eval` runs, up to MAX_NESTING scripts deep. Raises ValueError when a quote never closes."""
    lexer = Lexer(script)
    tokens = lexer.tokens()
    if lexer.unclosed:
        raise ValueError("a quote never closes")
    return commands_in(tokens, nesting)


def commands_in(tokens: list[Word | str], nesting: int) -> list[list[str]]:
    commands: list[list[str]] = []
    for command in split_commands(tokens):
        for inner in command.substitutions:
            commands += commands_in(inner, nesting)
        if command.words:
            commands.append(command.words)
            nested = nested_script(command_words(command.words)) if nesting < MAX_NESTING else None
            if nested is not None:
                inner = simple_commands(nested, nesting + 1)
                if any(isinstance(word, Expanded) for word in command.words):
                    inner = [[Expanded(word) for word in words] for words in inner]
                commands += inner
    return commands


def decide(command: str) -> tuple[str, str] | None:
    try:
        commands = simple_commands(command)
    except (ValueError, RecursionError):
        return ("ask", UNREADABLE_REASON) if PASSPHRASE_FORM.search(command) else None
    session_set = socket_set = False
    for words in commands:
        name_and_arguments = command_words(words)
        if not name_and_arguments or name_and_arguments[0] == "export":
            session_set = session_set or any(word.startswith(SESSION_ASSIGNMENT) for word in words)
        elif posixpath.basename(name_and_arguments[0]) == "envh":
            prefix = words[: len(words) - len(name_and_arguments)]
            in_session = session_set or any(word.startswith(SESSION_ASSIGNMENT) for word in prefix)
            socket_path, arguments = socket_and_arguments(name_and_arguments[1:], socket_set or any(SOCKET_ENV_VAR in word for word in prefix))
            reason = passphrase_reason(arguments, in_session)
            if reason is not None and not granted_at_once(reason, arguments, socket_path):
                return "ask", reason
        socket_set = socket_set or any(SOCKET_ENV_VAR in word for word in words)
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
