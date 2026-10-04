#!/usr/bin/env python3
"""envh hook for Claude Code (PreToolUse) and Cursor (beforeShellExecution). Standard library only.

Asks in the app exactly when a command will make the envh console ask for the vault passphrase, and refuses sudo
from the agent. It reads the command the way a shell does, so envh only counts where it runs: as a command name,
including after `&&`, `;`, a pipe or a newline, inside `$(...)`, backticks, `bash -c` or `eval`, behind wrappers like
`timeout` or `uv run`, or with a path prefix. envh inside an argument, a quoted string, a comment or a heredoc body is
a mention and never asks, unless the quoting cannot be read at all; then text that looks like such a request asks.
sudo, su, doas and pkexec are denied likewise, only where they would run, and more strictly: every argument of a
shell, eval or ssh and a heredoc fed to a shell are read as commands too.
A script file, an interpreter one-liner, a pipe into a shell or a variable-built command gets around it, and
nothing depends on it: a disguised session-less `envh run` still prompts on the envh console and a disguised
`envh session start` still needs the passphrase typed there. The hook is attention plus a second chance to deny, not
the boundary.
With no opinion it prints nothing, for Cursor too: a Cursor "allow" can skip Cursor's own approval of a command.
Install: see settings.snippet.json and cursor/hooks.snippet.json.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

PRIVILEGED = ("sudo", "su", "doas", "pkexec")
PRIVILEGE = re.compile(r"(?<![\w.-])(?:[\w./-]*/)?(?:" + "|".join(PRIVILEGED) + r")(?![\w./-])")
PASSPHRASE_FORM = re.compile(r"(?<![\w.-])(?:[\w./-]*/)?envh\s+(?:session\s+start|preset\s+propose|import|run)\b")
SESSION_ASSIGNMENT = "ENVH_SESSION="
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
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
RUN_REASON = "envh: running one command with secrets; approve it on the envh console with your vault passphrase"
UNREADABLE_REASON = "envh: this command's quoting could not be read, and it may start an envh request that needs your passphrase"

OPERATORS = ("<<<", "<<-", "&>>", ";;&", "&&", "||", ";;", ";&", "|&", ">>", ">|", ">&", "<&", "<>", "<<", "&>", ";", "&", "|", "<", ">", "(", ")", "\n")
SEPARATORS = {";", "&", "&&", "||", "|", "|&", ";;", ";&", ";;&", "(", ")", "\n"}
METACHARACTERS = set(" \t\n;&|<>()")
REDIRECT_FD = re.compile(r"\d+|\{\w+\}")
NUMBER = re.compile(r"\d+(?:\.\d+)?[smhd]?")
COMMAND_PREFIX_KEYWORDS = RESERVED_WORDS | {"coproc", "function"}
STRING_RUNNERS = SHELLS | {"ksh", "eval", "ssh"}
WRAPPER_VALUE_OPTIONS = {
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
FIND_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}


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


@dataclass
class Word:
    value: str = ""
    quoted: bool = False
    substitutions: list[list[Word | str]] = field(default_factory=list)
    heredoc: str | None = None


@dataclass
class Command:
    words: list[Word] = field(default_factory=list)
    stdin: list[str] = field(default_factory=list)
    substitutions: list[list[Word | str]] = field(default_factory=list)


class Lexer:
    """Splits shell text into words and operators the way bash does, closely enough to tell a command from data."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.heredocs: list[tuple[Word, bool]] = []

    def peek(self, length: int = 1) -> str:
        return self.text[self.pos : self.pos + length]

    def tokens(self, until_paren: bool = False) -> list[Word | str]:
        """Words and operators up to the end of the text, or with `until_paren` up to the `)` that closes a $(...) or an array."""
        tokens: list[Word | str] = []
        depth = 0
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
        return Lexer(content).tokens()

    def ansi_c_quoted(self) -> str:
        value = ""
        while self.pos < len(self.text) and self.peek() != "'":
            if self.peek() == "\\":
                self.pos += 1
            value += self.peek()
            self.pos += 1
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
                Lexer(delimiter.heredoc).expansions(delimiter.substitutions, closer="")
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
            command.words.append(token)
        redirect = ""
    return commands


def after_options(arguments: list[Word], value_options: set[str]) -> list[Word]:
    """Drops a wrapper's options, their values and any duration, to reach the command it runs."""
    index = 0
    while index < len(arguments) and (arguments[index].value.startswith("-") or NUMBER.fullmatch(arguments[index].value)):
        if arguments[index].value == "--":
            return arguments[index + 1 :]
        index += 2 if arguments[index].value in value_options else 1
    return arguments[index:]


def words_run_privileged(words: list[Word], stdin: list[str]) -> bool:
    """Whether one simple command runs sudo, su, doas or pkexec.

    A shell, eval or ssh runs text, so each argument, all of them joined, and the here-string or heredoc it reads are
    checked as commands too.
    """
    while words and (words[0].value in COMMAND_PREFIX_KEYWORDS or ASSIGNMENT.match(words[0].value)):
        words = words[2 if words[0].value == "function" else 1 :]
    if not words:
        return False
    name, arguments = words[0].value.rsplit("/", 1)[-1], words[1:]
    if name in PRIVILEGED:
        return True
    if name == "command" and arguments and arguments[0].value in ("-v", "-V"):
        return False
    if name in WRAPPER_VALUE_OPTIONS:
        return words_run_privileged(after_options(arguments, WRAPPER_VALUE_OPTIONS[name]), stdin)
    if name in STRING_RUNNERS:
        values = [word.value for word in arguments]
        return any(text_runs_privileged(text) for text in dict.fromkeys([*values, " ".join(values), *stdin]))
    if name == "find":
        return any(words_run_privileged(arguments[index + 1 :], []) for index, word in enumerate(arguments) if word.value in FIND_ACTIONS)
    return False


def text_runs_privileged(text: str) -> bool:
    return tokens_run_privileged(Lexer(text).tokens())


def tokens_run_privileged(tokens: list[Word | str]) -> bool:
    for command in split_commands(tokens):
        if any(tokens_run_privileged(inner) for inner in command.substitutions) or words_run_privileged(command.words, command.stdin):
            return True
    return False


def runs_privileged(command: str) -> bool:
    """Falls back to matching the words anywhere when the command is nested too deeply to parse."""
    try:
        return text_runs_privileged(command)
    except RecursionError:
        return PRIVILEGE.search(command) is not None


def decide(command: str) -> tuple[str, str] | None:
    if runs_privileged(command):
        return "deny", "envh policy: agents never run sudo, su, doas or pkexec; a cached credential could be reused. Ask the human to run it."
    try:
        commands = simple_commands(command)
    except (ValueError, RecursionError):
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
