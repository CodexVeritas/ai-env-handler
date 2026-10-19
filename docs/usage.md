# Daily use

## Daily use

```bash
envh list                                                        # secrets (names, policy, descriptions), presets, live sessions (not their ids)
envh manage                                                      # how to add, change or remove secrets and presets

# one command, one approval
envh run --preset news-bot-personal --reason "backfill" -- uv run python scripts/backfill.py

# several commands, one approval
envh session start news-bot-personal --minutes 120 --reason "backfill articles"
envh run --session <id> --reason "backfill" -- uv run python scripts/backfill.py
envh session end <id>
```

- `envh run` without a session asks the console for that one run and waits for your answer, then runs the command. It is the way to approve every request, and the only way to use secrets marked `per-run`, which sessions never cover.
- A key marked `auto` needs no approval. A run, or a session, whose keys are all `auto` starts at once, and the console only logs it. Set it on the console in the key's approval rule; `envh list` shows `auto` next to it. Take `auto` off a key and a session that opened without you stops covering it from its next run.
- A session is approved once; every `envh run --session` inside it is approved instantly and logged. The session lasts the minutes you asked for, capped by each secret's `max_session` and by 24 hours.
- `envh session start ... --quiet` prints only the id.
- Name several presets to combine them: `envh session start news-bot forecasting-bot ...`, or `envh run --preset news-bot,forecasting-bot`. You get all their variables; per-run beats session and the shortest `max_session` wins. If two presets map the same variable to different secrets, the request is refused: leave one preset out, or pick with `--with VAR=SECRET` (listing the other variables you need too).
- `--with` narrows a run to some of the session's variables, or maps variables ad hoc: `--with OPENAI_API_KEY,OPENROUTER_API_KEY=TEAM_OPENROUTER_KEY`.
- `--reason` is optional but the console shows its absence loudly. Write what you would want to read before approving.
- A running command is never killed when its session expires; expiry only stops new approvals. When the command itself exits, any process it left running is terminated so the values do not outlive the run (`--keep-background` to opt out).
- One user can have 32 `envh` commands running or waiting at once; another is refused until one finishes. A `--reason` can be up to 1,000 characters, and the audit log keeps the first 4,000 characters of a command line.
- `envh status` shows pending requests, active runs and live sessions as JSON. Neither it nor `envh list` shows session ids: `envh session start` prints yours, and the console's `/sessions` shows them all.
- If `envh session start` stops waiting before you approve (an agent's tool call timed out), the request still waits for 10 minutes. `envh session wait <code>` picks it up again, with the code `envh session start` printed. A request id from `envh status` is not enough, so another program running as you can't collect the session.

Exit codes: 3 broker not running, 4 denied, 5 request error (unknown preset, variable not in session), otherwise the command's own exit code.

## Policy and presets

Both policy files are owned by the `envh` user with mode 0600. Nothing running as you, agents included, can read or write them. The only ways they change:

| File | Who can change it | How |
|---|---|---|
| `config.yaml` (defaults, per-key rules and descriptions) | you, on the console | `/settings`, or descriptions and rules from `/keys`; `/edit-config` opens it in an editor and checks the result. Saving from `/settings` rewrites the file in envh's layout, with a comment on each option, so comments you add by hand are not kept |
| `presets.yaml` | you, on the console | `/presets` or `/edit-presets`, the import wizard, approving an agent's `envh preset propose` (shown as changes, approved with your passphrase), or a rename in `/keys` |
| the vault | you, on the console | `/keys` and `/add`, the import wizard |

`envh manage` lists the console commands for these changes and says whether the console is running.

An agent can only *propose*: `envh preset validate draft.yaml` checks a draft without changing anything, `envh preset propose draft.yaml --reason "..."` puts the diff on your console. A proposal that maps a variable to a secret the agent should not have is just a diff you deny. Root can of course edit the files directly; the console's `/reload` picks that up, and a save from the console refuses to overwrite a `config.yaml` changed on disk since.

`config.yaml`:

```yaml
# envh settings. Change them on the console with /settings; saving there rewrites this file.
users: [alice]            # logins whose scripts and agents may ask for keys
notify: true              # notification, chime and console bell while a request waits
defaults:                 # for every key without its own setting below
  approval: session       # session: one approval opens a session | per-run: ask on every run
  max_session: 1h         # longest session that may include a key; hard cap 24h
secrets:                  # per key: approval, max_session, description; the rest follows the defaults
  DATABASE_URL: {approval: per-run, description: "Production database, read-only user"}
  OPENAI_API_KEY: {max_session: 8h}
  FRED_API_KEY: {approval: auto, description: "Free economic data, read-only"}
```

`approval: auto` means no approval: any run gets the key at once, and every use is still logged. Only a single key's rule can say it, not `defaults:` or a preset, so a new key always needs approval until you mark it.

`/var/lib/envh/presets.yaml` is written only by the broker (import wizard, proposals, console):

```yaml
presets:
  news-bot-team:
    max_session: 3h
    env:
      GITHUB_TOKEN: GITHUB_TOKEN
      OPENROUTER_API_KEY: TEAM_OPENROUTER_API_KEY
  news-bot-personal:
    env:
      GITHUB_TOKEN: { secret: GITHUB_TOKEN, approval: per-run }
      OPENROUTER_API_KEY: PERSONAL_OPENROUTER_API_KEY
```

The same variable can point at different secrets in different presets; that replaces commenting lines in and out. Unknown fields, bad durations, caps over 24h and secrets missing from the vault are rejected, never warned about.

Everything on the console is described in [The console](console.md#the-console).
