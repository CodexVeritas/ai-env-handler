# The console

The console is the one window where you approve requests for keys and manage the vault. It runs as the `envh` user, so nothing running as you, agents included, can read it or type into it through its terminal.

- **Start it once per login, and leave it open.** Closing it ends every session, and requests fail until it runs again.
- **You don't have to watch it.** A desktop notification and a chime tell you when a request is waiting.
- **Approving always takes your vault passphrase.** Look for your console phrase before you type it.

## Start the console

Open a **separate terminal window** (not a terminal pane inside an AI tool) and run:

```bash
sudo envh-console
```

Or click the "envh console" launcher. Enter your sudo password, check the console phrase, enter the vault passphrase. If a window asks for the passphrase without showing the console phrase, close it: something is impersonating envh. Leave this window open; it is where you approve requests. Closing it ends all sessions.

**Why it locks your terminal.** The helper chowns your terminal to the service user while the console runs and restores it afterwards. sudo may run the console on a pseudo-terminal of its own and pass on what you type (its `use_pty` setting), so the helper locks both that one and your window's. The console refuses to start while either is still yours.

## Notifications

**You don't have to watch the console.** When a request needs your passphrase, envh shows a desktop notification with its own chime, and the console rings its bell.
- **It reminds you every 5 minutes while the request waits.** Each reminder updates the same notification ("has been waiting 5 minutes"), and the notification disappears once you approve or deny.
- **The notification comes from the program that asked.** That program runs as you, in your desktop session, and reminds you only while it is still waiting. A request from a script with no desktop session (cron, SSH) only rings the bell.
- **The chime is envh's own**, a short rising bell, so it isn't mistaken for another app. envh generates it and keeps it in your private runtime folder (`$XDG_RUNTIME_DIR`).
- **Its text is fixed.** It never shows the requester's reason, so a request can't use it to show you instructions.
- **To turn off the notifications, the chime and the bell, turn off Notifications under `/settings` on the console.** That sets `notify: false` in `config.yaml`.

## Using the console

The console is one full-screen terminal app, modeled on Claude Code's. The home screen lists what happened (requests, approvals, runs, changes), newest at the bottom, with a command line under it. Long lines wrap, PgUp and PgDn scroll back through this run, and `/logs` goes back through earlier runs too.

- **Type `/` for commands.** A list appears as you type; ↑↓ pick one, Tab completes it, Enter runs it. **↑ on an empty command line brings back earlier commands.** `?` shows every shortcut.
- **A request takes over the screen when it arrives**, with everything it would grant: who asked, the reason, the variables and keys, the duration, the command line and the process. Type your vault passphrase and press Enter to approve, or press ↓ (or Esc) and then Enter to deny. If something is being typed when it arrives, such as a key name, it waits until you finish (Enter or Esc); the status line says so. A request that is withdrawn while you type stays on screen and ignores what you type until Enter, so the rest of a passphrase never lands anywhere else.
- **Letters typed on the home screen do nothing and are not shown.** Only `/` starts a command, so a passphrase typed when no request is on screen never appears there.
- **A change asks for your vault passphrase once; for the next hour, changes ask only to be confirmed.** The header shows the time left, the console warns five minutes before it locks again, and `/lock` locks it at once. Approving a request and changing the passphrase always ask. A wrong passphrase is logged and stops typing for two seconds.

### Commands

| Command | What it is for |
|---|---|
| `/keys` | every key with the first and last few characters of its value (fewer for short values, at most a fifth, nothing under 12 characters), where it is used, how it is approved and its description. Enter opens a key: show its value, rename it, describe it, replace its value, give it its own approval rule, or remove it. F2 renames and Del removes from the list too. |
| `/add` | store a new key; the value is typed or pasted hidden |
| `/presets` | create, rename and remove presets, and change their variables: the key each reads, its approval, and the preset's longest session. Edits stay in a draft; Ctrl-S shows every change and asks for the passphrase once, and leaving with unsaved edits asks what to do with them. |
| `/settings` | every option in `config.yaml` with its value and what it does: notifications, allowed users, the default approval and longest session, and rules for single keys. Saved like presets. |
| `/sessions` | live sessions and the commands running with keys; Enter ends a session early |
| `/logs` | what happened, by day, newest at the bottom, including earlier runs of the console; ↑↓, PgUp/PgDn, Home and End scroll. It reads the last 2000 lines of `/var/lib/envh/audit.jsonl`, which keeps every event in full and is never trimmed; the top of the list says when the file has older events. |
| `/passphrase` | change the vault passphrase; the vault is encrypted again with it |
| `/lock` | end the unlocked hour now; the next change asks for the passphrase again |
| `/edit-config`, `/edit-presets` | open the file in `$VISUAL`, `$EDITOR`, nano or vi, whichever is found first owned by root and changeable only by root, since it runs as `envh`; what you save is checked and shown before it is used |
| `/reload` | read `config.yaml` and `presets.yaml` again, after root changed them |
| `/quit` | stop the console (so does Ctrl-C twice); live sessions end |

### Keys: showing, describing and renaming

**Showing a value.** Show puts the whole value on screen, where you can also select it with the mouse, and hides it again after 30 seconds or at Esc. The audit log records which key was shown; see [What envh does not do](security.md#what-envh-does-not-do) for what it exposes.

**Descriptions.** A key's description says what it is for ("Personal OpenAI account, news bot"). `envh list` shows it, so scripts and agents can pick the right key. Never put the key itself in it.

**A rename follows the key** everywhere envh refers to it: the vault, its rule and description in `config.yaml`, presets, live sessions and waiting requests. Two things keep the old name: the `# VAR -> envh secret NAME` comments in `.env` files you imported, and any command or script that names the key itself (`--with VAR=OLD_NAME`). A new name is refused while `config.yaml` still has settings for it, or while a live session or waiting request still uses it, since either would quietly attach to the renamed key. Removing a key removes its rule and description too.
