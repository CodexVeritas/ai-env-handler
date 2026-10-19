# Using envh with AI agents

## Using it with Claude Code

The wizard's "Connect Claude Code" step does this for you, and says so when a rerun finds the copies out of date. By hand, copy the pieces under `claude/`:

- `claude/skills/envh/SKILL.md` → `~/.claude/skills/envh/SKILL.md` (or a project's `.claude/skills/envh/`). It teaches the agent to run one command directly and to start a session only for several, to always give a reason, what to do when refused, and a list of things it must never do.
- `claude/hooks/envh_ask.py` plus the hooks block from `claude/settings.snippet.json` → your `settings.json`. The hook asks in the app only when a command will make the console ask for your vault passphrase: `envh session start`, `envh preset propose`, `envh import` (not `--dry-run`) and `envh run` without a session. For a run or session start, it first asks the broker whether those keys need approval at all, and stays quiet when they are all `auto`. It asks anyway when it can't be sure which keys or which broker: bash rewrites an option first (a `$(...)`, backticks or a `$`), or the command sets `ENVH_SOCKET`. So you are notified exactly when the console needs you. It reads the command the way a shell does: `envh` counts after `&&`, `;`, a pipe or a newline, inside `$(...)`, backticks, `bash -c` or `eval`, behind `timeout`, `nohup`, `env` or `uv run`, and with a path prefix. `envh` in an argument, a quoted string, a comment or a heredoc body, such as a commit message, a `grep` or a README being written, is a mention and never asks, unless it is in a `$(...)` or backticks that bash still runs there, as in double quotes or an unquoted heredoc. Only when the quoting can't be read at all does text that looks like one of those requests ask. Prefer rules only? The snippet has that variant too, with the caveat that plain rules match only the start of a command.

  A hook sees only the command string. A script file, an interpreter one-liner (`python -c "subprocess.run(['envh', ...])"`) or a variable-built command can get around it, which is why nothing depends on it: a disguised session-less run still prompts on the envh console, and a disguised session start still needs your passphrase typed there. The hook is attention and a second chance to deny; the console is the boundary.
- `claude/CLAUDE.snippet.md` → three lines for a project's `CLAUDE.md`. The wizard adds them to `~/.claude/CLAUDE.md` on the first connection only; updates replace the skill and hook and leave your `CLAUDE.md` as you edited it. When it lacks any of the current lines, the wizard shows them so you can add them yourself.

Agents can draft presets: they write a YAML file, run `envh preset validate draft.yaml`, then `envh preset propose draft.yaml --reason "..."`; you see a diff on the console and decide. They cannot edit the files.

## Using it with Cursor

The wizard's "Connect Cursor" step does this for you, and says so when a rerun finds the copies out of date. Cursor uses the same skill and hook as Claude Code. By hand:

- `claude/skills/envh/SKILL.md` → `~/.cursor/skills/envh/SKILL.md` (or a project's `.cursor/skills/envh/`).
- `claude/hooks/envh_ask.py` → `~/.cursor/hooks/envh_ask.py`, plus the block from `cursor/hooks.snippet.json` → `~/.cursor/hooks.json`. The hook reads Cursor's `beforeShellExecution` format and makes the same decisions as in Claude Code. The matcher keeps Cursor from starting it for commands that don't mention `envh`.

What differs from Claude Code:

- **Cursor's ask is less reliable.** Cursor has been [reported](https://forum.cursor.com/t/beforeshellexecution-hook-permissions-allow-ask-ignored-allow-list-takes-precedence/144244) to skip a hook's ask for a command on its own allowlist. As with Claude Code, nothing depends on the hook: the console still asks.
- **The hook never answers "allow".** In Cursor an allow can skip Cursor's own approval of a command. For commands it has no opinion on, such as `envh list`, it prints nothing; Cursor logs that as a hook failure, which does not block the command.
- **No rules file.** Cursor keeps user rules in its settings, so there is no counterpart to the `CLAUDE.md` lines. The skill carries the same instructions.
- **Cursor also reads Claude Code's setup.** With its "Third-Party Imports" setting on (the default), Cursor loads `~/.claude/skills` and the hooks in `~/.claude/settings.json` too. It does not act on an ask from a Claude Code hook, which is why Cursor gets its own hook entry. The two skill copies are identical.
