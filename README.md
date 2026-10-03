# envh

Human-approved secrets for scripts and AI coding agents, on Linux.

Your API keys stop living in `.env` files. They move into a vault owned by a separate Linux user, and every use of a key is approved by you on a console that nothing running as you can type into. You approve a **session** (a set of variables, for a number of minutes, with a reason); scripts and agents then run inside it. Everything is logged.

```
you / an agent                                   the envh console (its own terminal, owned by user `envh`)
──────────────────────────────────────────────   ─────────────────────────────────────────────────────────
envh session start weekly-report --minutes 90 \
     --reason "forecast digest: research step"   SESSION REQUEST #3   code 4821   pid 51234
                                                    reason:   "forecast digest: research step"
                                                    preset:   weekly-report
                                                    OPENAI_API_KEY      <- OPENAI_API_KEY
                                                    OPENROUTER_API_KEY  <- PERSONAL_OPENROUTER_KEY
                                                    duration: 1h (capped from 90m)
                                                    type 4821 to approve, n4821 to deny
                                                 4821
session 3f9c... approved, expires 15:02:11       approved #3
envh run --session 3f9c... --reason "fetch" \
     -- uv run python scripts/research.py        run_start run=4 vars=[...] pid=51301
```

## How a request flows, and where the boundaries are

![How an envh request flows: a session approval and a run, with the two trust zones](docs/request-flow.svg)

<details>
<summary>Diagram source (Mermaid), for editing</summary>

```mermaid
sequenceDiagram
    autonumber
    box rgb(70, 45, 45) Runs as you (uid 1000): your shell, or an agent acting as you. Nothing here can read the vault or type into the console.
        actor You as You in your terminal<br/>(or an agent)
        participant CLI as envh command<br/>(envh session start / envh run)
        participant Script as your script
    end
    box rgb(45, 70, 45) Runs as the service user envh: files mode 0600, memory not readable by uid 1000
        participant Broker as broker<br/>(envh serve)
        participant Console as console window<br/>(the broker's own terminal)
        participant Vault as vault + policy files
    end
    actor Approver as You at the<br/>console window

    You->>CLI: type: envh session start research --minutes 60 --reason "weekly digest"
    CLI->>Broker: request over the Unix socket /run/envh/ctl.sock
    Note over CLI,Broker: the only door through the boundary; the kernel attaches your pid and uid (informational)
    Broker->>Vault: check the preset, policies and caps
    Broker->>Console: print the request with a fresh 4-digit code
    Note over Console,Approver: only the keyboard reaches this terminal: TIOCSTI is disabled and the device is owned by envh
    Console-->>Approver: you see: reason, preset, variables, duration, the exact command line, the code
    Approver->>Console: type the code (or n + code to deny)
    Console->>Broker: decision
    Broker->>Vault: append to the audit log
    Broker-->>CLI: session id and expiry
    CLI-->>You: you see: "session 3f9c… approved, expires 15:02"

    You->>CLI: type: envh run --session 3f9c… --reason "fetch" -- python research.py
    CLI->>Broker: request with the session id
    Broker->>Vault: session live? variables covered? none per-run?
    Broker->>Vault: append run_start to the audit log
    Broker-->>CLI: the values for those variables (once, on this connection)
    CLI->>Script: start it with the values in its environment
    Note over Script: from here the values are visible to this process, its imports, and other processes running as you
    Script-->>CLI: exit code
    CLI->>Broker: run_done
    Broker->>Vault: append run_end to the audit log
```

</details>

What each line means for you:

- **Steps 1 and 9 are commands you (or an agent) type in an ordinary terminal.** The `envh` command is just a client; it holds no secrets and has no special rights.
- **Steps 3 to 6 happen in a different window**: the console that `sudo envh-console` opened. It is where every approval happens, and it is the only place.
- **Steps 8 and 12 are what the client does on your side.** It prints the session id; it never prints a value. Values go straight into the environment of the script you named, and nowhere else: not into your shell, not into a file.
- **After step 13 the values are gone.** They lived only in the environment of the script and of processes it started. When the script exits, `envh run` terminates any of those processes still running (it tags the run and finds them by that tag), so nothing that received the values survives the run; the audit log records how many were stopped. Pass `--keep-background` if a run is meant to leave a daemon behind, and treat that daemon as holding the values. What envh cannot undo is a copy the script itself made, for example into a log file.

The boundaries, in words:

| Boundary | What enforces it | What it means |
|---|---|---|
| **Red box vs green box** | Linux user separation. The vault, policy files and audit log are owned by `envh` with mode 0600; the broker is a different user, non-dumpable, with no secrets in its environment. | You, your scripts, and any agent running as you cannot read a secret, a policy or the log, and cannot change what is allowed. |
| **The socket** | A Unix socket in `/run/envh`, a directory only `envh` can write. The broker reads the connecting process's uid from the kernel (`SO_PEERCRED`, not spoofable) and only talks to logins listed under `users:` in `config.yaml`. | Anyone running as a listed user can *ask*. Asking never grants anything; it only puts a prompt on the console. Other local accounts are refused before they can see or request anything. |
| **Per-user sessions** | Every session and pending request is tagged with the kernel-reported uid that created it. | A session can be used, waited on, listed or ended only by the user who opened it. Two listed users on one machine cannot use each other's sessions; only the console sees everything. |
| **The console keyboard** | `TIOCSTI` disabled in the kernel, the terminal device chowned to `envh`, approval by a random code shown only there. | No process running as you can inject the keystroke that approves. A printed fake prompt fails because its code will not match. |
| **Not a boundary** | | During step 12 the value is in a process running as you, like any environment variable. A session id is a bearer token for its lifetime. The reason and the pid shown in the prompt are claims by the requester. On an X11 desktop, a same-user process with display access can type and read the screen; see Habits. |

## What it protects, and what it does not

**Kernel-enforced, against any process running as you (including every agent):**

- The vault, the policy files and the audit log are owned by the service user `envh` with mode 0600. You cannot read them; neither can an agent.
- The vault is encrypted at rest (age, passphrase mode) for the case of a stolen or copied disk.
- The broker's memory is unreadable: a different user, marked non-dumpable, with no secrets in its environment.
- Nothing can type into the console. Linux 6.2+ disables the `TIOCSTI` ioctl (checked at start), and the console's terminal device is chowned to `envh` before the broker starts.
- The code is root-owned under `/opt/envh`; the only client is `/usr/local/bin/envh`.

**What a key use means:** the approved command receives the real value in its environment. It is visible to that process, to everything it imports, and to every other process running as you while it runs. A malicious dependency no longer gets every key on disk whenever it likes; it gets the values of the one approved run it is part of, and you see that run in the log.

See [What envh does not do](#what-envh-does-not-do) before relying on it.

## Prerequisites

- Linux with a kernel of 6.2 or newer (`sysctl dev.tty.legacy_tiocsti` prints 0) and Yama (`sysctl kernel.yama.ptrace_scope` prints 1 or more). Ubuntu 22.04 and newer, Pop!_OS 22.04, Fedora, Arch and Debian 12 all qualify.
- `sudo` with a password. No `NOPASSWD` rules for your user.
- You must not be in a group that grants root without a password. The common one is `docker`: being in it means any process running as you can become root with `docker run -v /:/host`. Leave it with `sudo gpasswd -d $USER docker` and log out and in; use `sudo docker` afterwards. The installer refuses to proceed while you are in such a group.
- [uv](https://docs.astral.sh/uv/) installed for your normal user (`curl -LsSf https://astral.sh/uv/install.sh | sh`). Python 3.12 is fetched by uv if your system lacks it.
- A terminal application. GNOME Terminal gets a launcher icon; any other terminal works with one command.

## Install

```bash
git clone <this repository> envh && cd envh
sudo scripts/bootstrap.sh --dry-run     # prints everything it would do
sudo scripts/bootstrap.sh               # add --disable-sudo-cache to make sudo always ask (recommended, see Habits)
```

What the bootstrap does: copies the repository to a staging directory, creates `/opt/envh` (a root-owned Python 3.12 environment) and installs envh into it, links `/usr/local/bin/envh`, then runs `envh install`, which:

1. runs preflight checks (TIOCSTI, Yama, root-granting groups) and prints a warning if you are on an X11 session;
2. creates the service user `envh` with home `/var/lib/envh` (mode 0700);
3. installs `/usr/local/sbin/envh-console`, the root helper that starts the broker;
4. installs a desktop launcher named "envh console" if a desktop terminal is found;
5. optionally writes `/etc/sudoers.d/envh-no-credential-cache` (`--disable-sudo-cache`);
6. runs `envh init` as the service user: it writes `users: [<your login>]` into the policy, you choose the vault passphrase, and you get a **console phrase**.

Write the console phrase down. The real console prints it before asking for your passphrase. If a window asks for the passphrase without showing it, close it: something is impersonating envh.

Upgrade: stop the console, pull, rerun `sudo scripts/bootstrap.sh`. Uninstall: `sudo envh uninstall` (keeps the vault) or `sudo envh uninstall --purge`.

## Start the console

Open a **separate terminal window** (not a terminal pane inside an AI tool) and run:

```bash
sudo envh-console
```

Or click the "envh console" launcher. Enter your sudo password, check the console phrase, enter the vault passphrase. Leave this window open; it is where you approve requests. Closing it ends all sessions.

The helper chowns the terminal device to the service user for the duration and restores it afterwards.

## Move your `.env` files over

```bash
envh import --dry-run ~/code            # scan a directory (depth 3) and show the plan
envh import ~/code                      # or give specific files: envh import ~/code/bot/.env
```

The wizard walks through six steps and writes nothing until the last one:

1. **Files.** Lists every `.env` and `.env.*` found (not `.env.example`); choose all or some.
2. **Contents.** Parses active lines and commented-out assignments, and treats comment headers as **groups**. A file like this

   ```
   METACULUS_TOKEN=...
   LOG_LEVEL=info

   # MiniBench Mode
   OPENROUTER_API_KEY=...
   ASKNEWS_API_KEY=...

   # Ben Mode
   # OPENROUTER_API_KEY=...
   # ASKNEWS_API_KEY=...
   ```

   has a base group (`METACULUS_TOKEN`, `LOG_LEVEL`) and two mode groups. Each variable is classified as secret or config by its name and value; you can flip any.
3. **Names.** Secrets get vault names: `METACULUS_TOKEN`, `MINIBENCH_OPENROUTER_API_KEY`, `BEN_OPENROUTER_API_KEY`, and so on. Identical values anywhere share one entry; the same variable with different values in different repos gets the repo as prefix. Rename anything.
4. **Presets.** One preset per group, named `<repo>-<group>` (`auto-questions-minibench`, `auto-questions-ben`), each containing the base variables plus the group's. A repo without groups gets one preset named after it. Rename or drop presets.
5. **Plan.** Secrets to store (names and fingerprints only), presets in full, and a diff per file: secret lines become `# VAR -> envh secret NAME (presets: ...)`, headers and config lines stay as they are. Choose per file whether to rewrite it.
6. **Apply.** The console shows the same summary and asks for a code. Then the files are rewritten.

There is no plaintext backup, by design. To see a value again: `envh run --with VAR=SECRET --reason "recover" -- printenv VAR`, approved on the console.

## Find leftover copies

Keys end up in more places than `.env` files: shell history, Claude Code transcripts under `~/.claude/projects`, notebooks, scratch scripts, other repos, git remotes with embedded tokens. After importing, find them:

```bash
envh scan                      # your home directory
envh scan ~/code ~/Downloads   # specific places; add --json for machine-readable output
```

It prints the file, the line, what kind of key it looks like, the first four characters, the length and a short fingerprint, never the value. The fingerprint lets you see that the same key sits in several files. Exit code 1 means something was found. It skips binaries, vendor directories, caches, browser profiles and files over 25 MB (`--max-size`), and it looks inside `.git/config` but not git history.

What it can and cannot identify:

- **By value, anywhere:** OpenAI, OpenRouter, Anthropic, Perplexity, Google/Gemini, E2B, GitHub, Slack, AWS, Stripe, Hugging Face, Groq, Tavily, Replicate, Notion, SendGrid, JWTs, private keys, database URLs with passwords.
- **Only in a named assignment** (`FRED_API_KEY=...`, `serp_api_key: "..."`): SerpAPI, FRED, AskNews, Hyperbrowser, Metaculus, Exa, Mistral, Cohere, Together and other keys that are plain hex or random strings. A bare copy of one of these in a transcript has no distinctive shape and is not reported; search for it yourself with a few characters you remember, for example `grep -rl 'first8chars' ~/.claude`.
- **Not scanned:** git object history (`git log -p -S<prefix>` inside a repo), binary databases such as browser profiles and VS Code state, encrypted stores such as the Claude desktop app's local environment, and mounted drives outside the paths given.

The scan stays separate from the import wizard on purpose: the wizard moves values out of files you chose, the scan is a read-only audit of everything else. The wizard prints a reminder to run it.

## Daily use

```bash
envh list                                                        # secrets (names, policy), presets, live sessions
envh session start auto-questions-ben --minutes 120 --reason "backfill questions"
envh run --session <id> --reason "backfill" -- uv run python scripts/backfill.py
envh session end <id>
```

- A session is approved once; every `envh run --session` inside it is approved instantly and logged. The session lasts the minutes you asked for, capped by each secret's `max_session` and by 24 hours.
- `export ENVH_SESSION=<id>` lets you omit `--session`. `envh session start ... --quiet` prints only the id.
- `envh run` without a session prompts on the console for that one run.
- `--with` narrows a run to some of the session's variables, or maps variables ad hoc: `--with OPENAI_API_KEY,OPENROUTER_API_KEY=MINIBENCH_OPENROUTER_KEY`.
- `--reason` is optional but the console shows its absence loudly. Write what you would want to read before approving.
- A running command is never killed when its session expires; expiry only stops new approvals. When the command itself exits, any process it left running is terminated so the values do not outlive the run (`--keep-background` to opt out).
- `envh status` shows pending requests, active runs and live sessions as JSON.

Exit codes: 3 broker not running, 4 denied, 5 request error (unknown preset, variable not in session), otherwise the command's own exit code.

## Policy and presets

Both policy files are owned by the `envh` user with mode 0600. Nothing running as you, agents included, can read or write them. The only ways they change:

| File | Who can change it | How |
|---|---|---|
| `config.yaml` (defaults, per-secret policy) | you, on the console | `edit config` opens it in an editor; the result is validated and shown as a diff before it is saved |
| `presets.yaml` | you, on the console | `edit presets`, `preset rm NAME`, the import wizard, or approving an agent's `envh preset propose` (shown as a diff, approved with a code) |
| the vault | you, on the console | `add`, `rm`, the import wizard |

An agent can only *propose*: `envh preset validate draft.yaml` checks a draft without changing anything, `envh preset propose draft.yaml --reason "..."` puts the diff on your console. A proposal that maps a variable to a secret the agent should not have is just a diff you deny. Root can of course edit the files directly; the console `reload` command picks that up.

`config.yaml`:

```yaml
users: [alice]           # login names allowed to talk to the broker; the installer fills in yours
defaults:
  approval: session      # session: one approval opens a session | per-run: ask on every run, never in sessions
  max_session: 1h        # hard cap 24h
secrets:
  DATABASE_URL: { approval: per-run }
  OPENAI_API_KEY: { max_session: 8h }
```

`/var/lib/envh/presets.yaml` is written only by the broker (import wizard, proposals, console):

```yaml
presets:
  auto-questions-minibench:
    max_session: 3h
    env:
      METACULUS_TOKEN: METACULUS_TOKEN
      OPENROUTER_API_KEY: MINIBENCH_OPENROUTER_API_KEY
  auto-questions-ben:
    env:
      METACULUS_TOKEN: { secret: METACULUS_TOKEN, approval: per-run }
      OPENROUTER_API_KEY: BEN_OPENROUTER_API_KEY
```

The same variable can point at different secrets in different presets; that replaces commenting lines in and out. Unknown fields, bad durations, caps over 24h and secrets missing from the vault are rejected, never warned about.

Console commands: `<code>` approve, `n<code>` deny, `add SECRET` (value typed hidden), `rm SECRET`, `secrets`, `presets`, `sessions`, `runs`, `pending`, `preset rm NAME`, `edit config`, `edit presets` (add an editor name to override `$VISUAL`/`$EDITOR`/nano/vi), `reload`, `help`, `quit`.

## Using it with Claude Code

Copy the pieces under `claude/`:

- `claude/skills/envh/SKILL.md` → `~/.claude/skills/envh/SKILL.md` (or a project's `.claude/skills/envh/`). It teaches the agent the session flow, to always give a reason, what to do when refused, and a list of things it must never do.
- `claude/hooks/envh_ask.py` plus the hooks block from `claude/settings.snippet.json` → your `settings.json`. The hook turns `envh session start`, `envh preset propose`, `envh import` and session-less `envh run` into in-app "ask" prompts, so you are notified exactly when the console needs you, and it denies any `sudo`, `su`, `doas` or `pkexec` the agent attempts. It fails closed: `envh` reached through `&&` chains, subshells, `bash -c`, interpreter one-liners, or a path prefix also asks, as does any invocation it does not recognize. Prefer rules only? The snippet has that variant too, with the caveat that plain rules match only the start of a command.

  A hook sees only the command string. A script file or a variable-built command can get around it, which is why nothing depends on it: a disguised session-less run still prompts on the envh console, and a disguised session start still needs the code typed there. The hook is attention and a second chance to deny; the console is the boundary.
- `claude/CLAUDE.snippet.md` → three lines for a project's `CLAUDE.md`.

Agents can draft presets: they write a YAML file, run `envh preset validate draft.yaml`, then `envh preset propose draft.yaml --reason "..."`; you see a diff on the console and decide. They cannot edit the files.

## Habits that keep this safe

- **Never type `sudo` in a terminal an agent can reach**, such as the terminal pane of the Claude desktop app. sudo caches your credential for that terminal for several minutes, and the agent could run `sudo -n <anything>` there. Run privileged commands in a separate window, and consider `sudo -k` right after, or install with `--disable-sudo-cache` so sudo always asks.
- Start the console in its own window. Never inside an agent's terminal pane.
- Keep `NOPASSWD` out of sudoers and stay out of the `docker` group.
- Read the whole prompt before typing a code: the reason, the variables, the duration, the command line.
- Do not paste secrets into chats, commits, logs or command lines.
- Treat `/var/lib/envh` as secret material if you ever copy or back it up. Back up `vault.age` only to encrypted media; recovering it needs only the standard `age` tool: `age -d vault.age`.
- On X11 (`echo $XDG_SESSION_TYPE`), any process running as you can type into windows and read the screen. Options, strictest first: run the console on a text console (Ctrl-Alt-F3), use a Wayland session, or enable Claude Code's Bash sandbox so agent commands cannot reach the X socket.

## What envh does not do

- **It does not hide a value from the command that receives it.** Values are visible to that process, everything it imports, and every other process running as you for the duration of the run. envh cannot revoke a value once delivered, cannot undo a copy the command made (a log line, a file, a cache), and a session expiring does not stop a running command. It does end the run cleanly: processes the command left behind are terminated when it exits.
- **It does not isolate an agent from your other repositories, your home directory, or the network.** That needs a container or VM around the agent, which is a separate project. On the host, your agent's own permission rules and classifier are the only control over what else it reads.
- **A session id is a bearer capability within your own account.** Anything running as you that obtains it, for example from a process environment, can use it until it expires. Keep sessions short and specific. Other local accounts cannot use it: the broker checks the kernel-reported uid of every connection against the session's owner.
- **Reasons and provenance are claims.** The `--reason` text and the pid and command line shown in prompts come from the requester and help you decide; they are not verified.
- **The console is only as trusted as your display and terminal.** On X11, same-user processes can type and read the screen. The desktop launcher can be shadowed by a file in `~/.local/share/applications`; the console phrase makes an impostor recognizable, it does not prevent one.
- **It does not stop a persistent same-user attacker from phishing you later.** An agent that can edit your shell startup files or desktop configuration could capture the sudo password or vault passphrase at the next start. Claude Code's protected-path rules reduce this; only running agents in a container removes it.
- **The vault is only as strong as the passphrase**, and copies of `vault.age` or `audit.jsonl` outside `/var/lib/envh` must be treated as secrets. The audit log holds command lines and reasons, never values.
- **The import wizard rewrites `.env` files in place with no plaintext backup.** It shows the exact diff first and leaves config lines untouched.
- **Requirements it will refuse without:** Linux 6.2+ with `legacy_tiocsti=0`, Yama ptrace scope 1 or more, sudo with a password, and your user outside root-granting groups.
- **Not provided:** rotating or revoking keys at the provider, per-request network allow-lists, rate limits, remote use, macOS or Windows support, protection against root or physical access while the broker is running. Several local users can be listed in `users:` and each gets their own sessions, but they share one vault, one policy and one console.

## Development

```bash
uv sync --all-groups
uv run pytest -q                       # no root, no network, no environment variables needed
uv run envh --help
```

Run a development broker as yourself, without installing: `uv run envh init --data-dir /tmp/envh-dev`, then in a terminal `uv run envh serve --data-dir /tmp/envh-dev --socket /tmp/envh-dev/ctl.sock`, and point clients at it with `--socket /tmp/envh-dev/ctl.sock` or `ENVH_SOCKET=/tmp/envh-dev/ctl.sock`. This gives none of the user-separation guarantees; it is for working on envh itself.

Layout: `src/envh/client.py`, `src/envh/importer.py` and `src/envh/scanner.py` are standard-library only (a test enforces it). `src/envh/server/` holds the broker: `config.py` (policy files), `vault.py` (age encryption), `state.py` (requests, sessions, runs), `broker.py` (decisions and side effects), `control.py` (Unix-socket protocol), `console.py` (prompts and admin commands), `serve.py` (startup and hardening), `install_cmd.py` (system setup).
