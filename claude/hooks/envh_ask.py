#!/usr/bin/env python3
"""Claude Code PreToolUse hook for envh. Standard library only.

Turns the moments the envh console needs a human into in-app "ask" prompts, and refuses sudo from the agent.
It fails closed: any mention of envh that is not a recognized safe form asks. A hook only sees the command
string, so a script file or a variable-built command can get around it; nothing depends on it. A disguised
session-less `envh run` still prompts on the envh console and a disguised `envh session start` still needs the
code typed there. The hook is attention plus a second chance to deny, not the boundary.
Install: see settings.snippet.json.
"""

import json
import re
import sys

WORD_START = r"(?<![\w.-])(?:[\w./-]*/)?"
WORD_END = r"(?![\w./-])"
ENVH = re.compile(WORD_START + r"envh" + WORD_END)
PRIVILEGE = re.compile(WORD_START + r"(?:sudo|su|doas|pkexec)" + WORD_END)
SEGMENT_END = re.compile(r"[;&|`)]|\$\(")
SAFE_FORMS = (
    re.compile(r"^\s+(?:list|status|--help|-h)\b"),
    re.compile(r"^\s+session\s+(?:wait|end)\b"),
    re.compile(r"^\s+preset\s+validate\b"),
)
ASK_FORMS = (
    (re.compile(r"^\s+session\s+start\b"), "envh: starting a secret session; approve it on the envh console (code shown there)"),
    (re.compile(r"^\s+preset\s+propose\b"), "envh: proposing a preset change; review the diff on the envh console"),
    (re.compile(r"^\s+import\b"), "envh: importing secrets; this is an interactive wizard meant for a human"),
)
RUN_FORM = re.compile(r"^\s+run\b")
SESSION_PREFIX = re.compile(r"(?<![\w.-])ENVH_SESSION=")


def segment(rest: str) -> str:
    """The arguments that belong to this envh invocation: up to the next shell operator."""
    match = SEGMENT_END.search(rest)
    return rest if match is None else rest[: match.start()]


def classify_invocation(rest: str, whole_command: str) -> tuple[str, str] | None:
    for form in SAFE_FORMS:
        if form.match(rest):
            return None
    for form, reason in ASK_FORMS:
        if form.match(rest):
            return "ask", reason
    if RUN_FORM.match(rest):
        if "--session" in segment(rest) or SESSION_PREFIX.search(whole_command):
            return None
        return "ask", "envh: running with secrets outside a session prompts on the envh console for every run; approve here first"
    return "ask", "envh: unrecognized way of invoking envh; approve only if you understand exactly what it does"


def decide(command: str) -> tuple[str, str] | None:
    if PRIVILEGE.search(command):
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
