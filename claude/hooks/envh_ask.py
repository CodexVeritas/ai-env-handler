#!/usr/bin/env python3
"""Claude Code PreToolUse hook for envh. Standard library only.

Turns the moments the envh console needs a human into in-app "ask" prompts, and refuses sudo from the agent.
It fails closed: any mention of envh that is not a recognized safe form asks. sudo, su, doas and pkexec are
denied where the shell would run them: as a command, behind a wrapper such as env or xargs, or in text a shell
runs ($(...), backticks, bash -c, eval, ssh, a heredoc fed to a shell). As data, such as a quoted argument, a
commit message or a heredoc for cat or python, they pass. A hook only sees the command string, so a script
file, an interpreter one-liner, a pipe into a shell or a variable-built command can get around it; nothing
depends on it. A disguised session-less `envh run` still prompts on the envh console and a disguised
`envh session start` still needs the passphrase typed there. The hook is attention plus a second chance to
deny, not the boundary.
Install: see settings.snippet.json.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass, field

WORD_START = r"(?<![\w.-])(?:[\w./-]*/)?"
WORD_END = r"(?![\w./-])"
ENVH = re.compile(WORD_START + r"envh" + WORD_END)
PRIVILEGED = ("sudo", "su", "doas", "pkexec")
PRIVILEGE = re.compile(WORD_START + r"(?:" + "|".join(PRIVILEGED) + r")" + WORD_END)
SEGMENT_END = re.compile(r"[;&|`)]|\$\(")
SAFE_FORMS = (
    re.compile(r"^\s+(?:list|status|--help|-h)\b"),
    re.compile(r"^\s+session\s+(?:wait|end)\b"),
    re.compile(r"^\s+preset\s+validate\b"),
)
ASK_FORMS = (
    (re.compile(r"^\s+session\s+start\b"), "envh: starting a secret session; approve it on the envh console with your vault passphrase"),
    (re.compile(r"^\s+preset\s+propose\b"), "envh: proposing a preset change; review the diff on the envh console"),
    (re.compile(r"^\s+import\b"), "envh: importing secrets; this is an interactive wizard meant for a human"),
)
RUN_FORM = re.compile(r"^\s+run\b")
SESSION_PREFIX = re.compile(r"(?<![\w.-])ENVH_SESSION=")

OPERATORS = ("<<<", "<<-", "&>>", ";;&", "&&", "||", ";;", ";&", "|&", ">>", ">|", ">&", "<&", "<>", "<<", "&>", ";", "&", "|", "<", ">", "(", ")", "\n")
SEPARATORS = {";", "&", "&&", "||", "|", "|&", ";;", ";&", ";;&", "(", ")", "\n"}
METACHARACTERS = set(" \t\n;&|<>()")
REDIRECT_FD = re.compile(r"\d+|\{\w+\}")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*(?:\[[^\]]*\])?\+?=")
NUMBER = re.compile(r"\d+(?:\.\d+)?[smhd]?")
COMMAND_PREFIX_KEYWORDS = {"!", "{", "if", "then", "elif", "else", "do", "while", "until", "coproc", "function"}
STRING_RUNNERS = {"bash", "sh", "dash", "zsh", "ksh", "eval", "ssh"}
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


def segment(rest: str) -> str:
    """The arguments that belong to this envh invocation: up to the next shell operator, and not the command after `--`."""
    match = SEGMENT_END.search(rest)
    own = rest if match is None else rest[: match.start()]
    return own.split(" -- ", 1)[0]


def has_session_flag(arguments: str) -> bool:
    try:
        tokens = shlex.split(arguments)
    except ValueError:
        return False
    return any(token == "--session" or token.startswith("--session=") for token in tokens)


def classify_invocation(rest: str, whole_command: str) -> tuple[str, str] | None:
    for form in SAFE_FORMS:
        if form.match(rest):
            return None
    for form, reason in ASK_FORMS:
        if form.match(rest):
            return "ask", reason
    if RUN_FORM.match(rest):
        if has_session_flag(segment(rest)) or SESSION_PREFIX.search(whole_command):
            return None
        return "ask", "envh: running with secrets outside a session prompts on the envh console for every run; approve here first"
    return "ask", "envh: unrecognized way of invoking envh; approve only if you understand exactly what it does"


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


def simple_commands(tokens: list[Word | str]) -> list[Command]:
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
    for command in simple_commands(tokens):
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
    verdict: tuple[str, str] | None = None
    for match in ENVH.finditer(command):
        verdict = classify_invocation(command[match.end():], command) or verdict
    return verdict


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    decision = decide(command)
    if decision is None:
        return 0
    verdict, reason = decision
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": verdict, "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
