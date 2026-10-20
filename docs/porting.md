# Porting envh to macOS

envh runs on Linux only. **macOS looks close**: it has the same building blocks (users, Unix sockets, sudo), mostly under different names. **Windows is not planned.**

If you're on a Mac and want envh, you can port it in your own fork with an AI agent, test it on your machine, and send it back as a pull request.

## How to try it

1. **Fork** [the repository](https://github.com/CodexVeritas/ai-env-handler) on GitHub and clone your fork.
2. **Open Claude Code in the clone** and paste the prompt below. It starts with `ultracode`, which lets Claude Code split the work across many agents. That is faster on a job this size, but it uses many more tokens; leave the word out to work with one agent.
3. **Test with fake keys first.** The prompt has the agent use a development broker, which needs no install; see [development.md](development.md).
4. **Open a pull request** to this repository when it works. Expect a careful review: these are the parts that keep keys away from agents.

## The prompt

```text
ultracode

Port envh, in this repository, to macOS without weakening its security model. envh is Linux only today. Its protection rests on four things: a separate service user that owns the vault, a Unix socket that reads the caller's uid from the kernel, a broker process that other users can't read, and a console whose terminal no process running as me can type into. Read README.md, docs/security.md and docs/porting.md first. docs/porting.md lists the Linux-specific pieces that are known.

Goals:
1. `uv run pytest -q` passes on macOS, and the Linux code paths behave exactly as before.
2. The setup wizard installs envh on macOS, and the console approves a request end to end, using fake keys only.
3. Every guarantee in docs/security.md either holds on macOS or is listed, with the reason, in a new "macOS" section of docs/security.md.

How to work:
- First research each Linux mechanism's macOS equivalent, and write the findings to docs/macos-port-notes.md before changing code. Mark anything you could not verify on this machine.
- Keep platform differences in src/envh/platform.py and src/envh/install/, behind small functions, not scattered through the code.
- Test with a development broker (docs/development.md) and fake keys. Never read my .env files, my keys, or environment variables.
- Never run sudo yourself. When a step needs root, give me the command and I'll run it in a separate terminal window.
- Ask me before installing any package or changing a system setting.
- When the tests pass, open a draft pull request against CodexVeritas/ai-env-handler. In it, list what you verified on this Mac and what you couldn't.
```

## What's Linux-specific

The macOS column is a starting point for the research, not a verified answer.

| Piece | Where | On Linux | Starting point on macOS |
|---|---|---|---|
| Who is calling the socket | `platform.py`: `peer_credentials` | `SO_PEERCRED` | `getpeereid()` or `LOCAL_PEERCRED` |
| Broker memory others can't read | `platform.py`: `make_non_dumpable` | `prctl(PR_SET_DUMPABLE, 0)` | `ptrace(PT_DENY_ATTACH)`, the hardened runtime |
| No typing into the console's terminal | `platform.py`: `preflight_problems` | `dev.tty.legacy_tiocsti=0`, the terminal chowned to `envh` | whether `TIOCSTI` is restricted, and whether the chown still blocks it |
| Same-user debugging blocked | `platform.py`: `preflight_problems` | Yama `ptrace_scope` ≥ 1 | `task_for_pid` rules, System Integrity Protection |
| Groups that grant root | `platform.py`: `ROOT_GRANTING_GROUPS` | `docker`, `lxd`, `disk` | which groups, if any, grant root without a password |
| Process parents and terminals | `platform.py`: `parent_and_terminal`; the console helper's `ps` loop | `/proc/<pid>/stat` | `ps`, `sysctl` or `libproc` |
| Ending processes a run left behind | `client/commands.py`: `processes_with_marker` | reads `/proc/<pid>/environ` | macOS hides other processes' environments: use process groups or another tag |
| The service user | `install/command.py` | `useradd --system` | `sysadminctl` or `dscl`, a hidden user |
| Runtime folder | `platform.py`: `RUNTIME_DIR` | `/run/envh` | `/var/run/envh` |
| The console helper | `install/command.py`: `HELPER_SCRIPT` | GNU `stat -c`, `runuser` | BSD `stat -f`, `sudo -u` |
| The app launcher | `install/command.py`: `DESKTOP_ENTRY` | a `.desktop` file | an `.app` or `.command` file |
| Notifications and the chime | `client/attention.py` | `gdbus` and freedesktop notifications | `osascript` notifications, or a small helper app |
| The setup wizard | `scripts/setup_wizard.py` | refuses other systems; `/usr/bin/python3`; `gpasswd`; `~/.config/Code` | allow macOS; `/usr/bin/python3` is a stub until the Xcode tools are installed; `dseditgroup`; `~/Library/Application Support/Code` |
| The scan's skip rules | `tools/scanner.py` | Linux cache and app folders | `~/Library/Caches`, `~/Library/Application Support` |
