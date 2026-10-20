# envh

**Human-approved API keys for scripts and AI coding agents, on Linux.**

Your API keys leave your `.env` files and move into an encrypted vault that neither you nor your agents can read directly. When a script or agent needs a key, it asks. You approve in a console window with your passphrase, and the key goes to that one command only. Every use is logged.

https://github.com/user-attachments/assets/d9ada2fc-4999-4a00-a756-6ed6b49f5aa6

A five-minute walkthrough of setup and daily use, recorded with fake keys. If it doesn't play, [download it](docs/demo.mp4).

> **Status:** early, and Linux only. Largely AI-written, then tested and red-teamed with AI (two Opus 4.8 ultracode sessions); it has had no independent security review. It is defense in depth, not a guarantee: see [what it covers and what it doesn't](#what-it-covers-and-what-it-doesnt).

## Why

A `.env` file can be read by everything that runs as you: every agent, every script, and every package those scripts import. One prompt injection or one bad dependency can take every key at once, and nothing tells you.

With envh:

- **Keys aren't in your files anymore.** The vault belongs to a separate Linux user, so nothing running as you can read it.
- **You approve every use**, in a console that agents can't type into, unless you mark a key `auto`.
- **A leak is smaller.** A bad script gets only the keys of the one run you approved, not everything on disk.
- **You can see what happened.** Every request, approval and run is logged.
- **Leftover copies get cleaned up.** A scan finds keys left in shell history, agent transcripts, editor history and other repos, and replaces them in bulk.

## How it works

**1. You, or an agent, ask for keys** in an ordinary terminal:

```bash
envh session start weekly-report --minutes 90 --reason "forecast digest: research step"
```

**2. The envh console shows the request.** It runs full screen in its own window, as its own user:

```
╭─ Session request #3 ───────────────────────────────────── waiting 0:12 ─╮
│ ben asks for a 1h session with weekly-report                             │
│                                                                          │
│ Reason    "forecast digest: research step"                               │
│ Keys      OPENAI_API_KEY     ← OPENAI_API_KEY                            │
│           OPENROUTER_API_KEY ← PERSONAL_OPENROUTER_KEY  Personal account │
│ Duration  1h  (asked for 90m; key and preset limits cap it)              │
│ Command   envh session start weekly-report --minutes 90 --reason '…'     │
│ Process   pid 51234 · /usr/bin/python3 /usr/local/bin/envh session …     │
│                                                                          │
│ › Approve  [amber basil cedar] Vault passphrase: (hidden)                │
│   Deny                                                                   │
╰──────────────────────────────────────────────────────────────────────────╯
```

**3. You type your vault passphrase** and press Enter. The words in brackets are your console phrase: type the passphrase only where you see them. Back in the terminal:

```
session brave-otter approved, expires 15:02:11
```

**4. Scripts and agents run inside the session** without asking again until it expires:

```bash
envh run --session brave-otter --reason "fetch" -- uv run python scripts/research.py
```

For a single command, skip the session: `envh run --preset weekly-report --reason "fetch" -- ...` asks for that one run.

The keys go straight into that command's environment, never into your shell or a file, and are gone when it exits. [docs/security.md](docs/security.md) has the diagram and the details.

## What it covers and what it doesn't

**It covers:**

- **Agents and scripts reading keys from disk.** The vault, the approval rules and the log belong to the `envh` user, and the kernel enforces that.
- **Using a key without your OK.** Every request waits for your vault passphrase on the console, unless you marked the key `auto`.
- **Typing into the console through its terminal.** Nothing running as you can inject keystrokes there, and every prompt shows your console phrase, so a fake prompt elsewhere stands out.
- **A stolen or copied disk.** The vault is encrypted with your passphrase.
- **Most leftover copies.** The scan finds keys left in text files across your home folder.

**It doesn't cover:**

- **What an approved command does with a key.** It gets the real value and can copy it anywhere. You approve a command line, not the code it runs, so review what you run.
- **A program watching your screen and keyboard.** On an X11 desktop, or with a writable `/dev/uinput`, a program running as you can record your passphrase and approve on its own. See [Who else can see and type into the console](docs/security.md#who-else-can-see-and-type-into-the-console).
- **Isolating agents.** They can still read your other files and use the network. That needs a container or VM.
- **Root, or physical access while the console runs.**
- **Every copy of a key.** The scan can't read databases (such as SQLite), other binary files or git history. Rotate any key that may have leaked.
- **Rotating keys at the provider, sharing keys across a team, Mac or Windows.**

The full list: [What envh does not do](docs/security.md#what-envh-does-not-do).

## Requirements

- **Linux 6.2 or newer.** Check with `uname -r`.
- **sudo that asks for your password.**
- **Ideally, not in the `docker` group.** Being in it is the same as having root without a password, so an agent could use it to read your keys. The wizard offers to take you out.
- **Mac:** not yet, but it looks close. You can [port it in a fork](docs/porting.md). **Windows:** not planned.

## Install

Open a terminal window of its own (not a terminal inside Claude, Cursor or VS Code), then run:

```bash
git clone https://github.com/CodexVeritas/ai-env-handler.git envh && cd envh
python3 scripts/setup_wizard.py
```

The wizard takes about 10 minutes, plus 15 for an optional scan. It asks before each change, and `?` at any question explains it. It installs envh, starts the console, moves a project's `.env` into the vault, connects Claude Code and Cursor, and scans for leftover keys.

[docs/install.md](docs/install.md) lists what it changes and how to fix a failed system check. To upgrade later, `git pull` and run the wizard again.

## Daily use

**1. Start the console** once per login, in its own window, and leave it open:

```bash
sudo envh-console
```

**2. Run things that need keys.** Once the wizard connects your agents, they do this for you.

```bash
envh list                                                                   # keys and presets, never values
envh run --preset my-app --reason "nightly sync" -- uv run python sync.py   # one command, one approval
envh session start my-app --minutes 60 --reason "debug the sync"            # several commands, one approval
envh run --session <id> --reason "retry" -- uv run python sync.py
```

**3. Approve on the console** when it chimes: read the request, type your passphrase, press Enter.

More in [docs/usage.md](docs/usage.md) (sessions, presets, approval rules) and [docs/console.md](docs/console.md) (every console command).

## Using it with AI agents

- **Claude Code and Cursor:** the wizard connects them. Each gets a skill that teaches it to ask for keys through envh, and a hook that warns you in the app before a request reaches the console.
- **Other agents** (Codex, Windsurf and others): paste the lines from [`claude/CLAUDE.snippet.md`](claude/CLAUDE.snippet.md) into their rules file.

Details: [docs/agents.md](docs/agents.md).

## Documentation

| Doc | What's in it |
|---|---|
| [install.md](docs/install.md) | requirements, the wizard's steps, failed system checks, what the install changes, upgrading, uninstalling |
| [usage.md](docs/usage.md) | runs, sessions, presets, approval rules, exit codes |
| [console.md](docs/console.md) | starting the console, notifications, every command |
| [import.md](docs/import.md) | moving `.env` files into the vault, and the backup it leaves |
| [scan.md](docs/scan.md) | finding leftover copies, what the scan misses, replacing copies |
| [agents.md](docs/agents.md) | Claude Code, Cursor and other agents |
| [security.md](docs/security.md) | how it works, its boundaries, what it doesn't do, safe habits, roadmap |
| [porting.md](docs/porting.md) | porting envh to macOS |
| [development.md](docs/development.md) | tests, a development broker, the code layout |

## FAQ

**Do I have to watch the console?**
No. A desktop notification and a chime tell you when a request is waiting, and remind you every 5 minutes.

**What happens when the console isn't running?**
Requests fail with exit code 3, and agents are told to ask you to start it. Keys marked `auto` need it running too.

**What if I forget my vault passphrase?**
There's no reset, because it's stored nowhere. Run `sudo envh uninstall --purge`, install again and add your keys again.

**Can my agent see my keys?**
Only the keys of a command you approved. That command gets the real values in its environment; while it runs, other programs running as you could read them too. When it exits they're gone, unless it copied them somewhere.

**Where did my `.env` values go?**
Into the vault. Each line in the file now names the vault key that replaced it. The original file is backed up under `~/envh-backups-and-scans/import-backups/` until you delete it, which you should do once your project works.

## Reporting a vulnerability

Please report privately, not in a public issue. See [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
