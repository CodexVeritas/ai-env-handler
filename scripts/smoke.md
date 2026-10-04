# Manual smoke test (not automated: needs root, a terminal and a desktop)

1. `python3 scripts/setup_wizard.py`: pick "Every detail" once and check each step lists what it changes; type `?` at a question in "Essentials only". Answer n at the install question and check nothing changed (no `/opt/envh`, no `envh` user), then rerun and install. Choose a vault passphrase; note the console phrase. `ls -l /usr/local/bin/envh` points into `/opt/envh/env`, and `/opt/envh/installed-from` names the commit.
2. Open a separate terminal (not the Claude desktop pane): `sudo envh-console`. Expect: console phrase, passphrase prompt, "console ready".
3. In your own terminal: `envh list` shows no secrets. `cat /var/lib/envh/vault.age` is denied.
4. `envh import --dry-run ~/code/some-project` on a repo with `# Mode` headers; check names and presets; rerun without `--dry-run`, approve on the console, check the plan lists each file's new lines and no values, check the original is in the printed backup folder, then delete that folder.
5. In the console, type `keys`: arrows move, F2 renames. Rename a key a preset uses; the passphrase is asked once; `envh list` shows the new name in the preset; Esc brings back the console with the held log lines.
6. `envh session start <preset> --minutes 30 --reason "smoke"`; approve with the vault passphrase (the prompt shows the console phrase); `envh run --session <id> --reason "smoke" -- env | grep -c KEY`.
7. `envh run --with SOME_SECRET --reason "one-off" -- true`; approve; confirm `audit.jsonl` has request/decision/run lines.
8. At a request, type a wrong passphrase: "wrong passphrase", the request stays pending, nothing you typed shows on screen, and `audit.jsonl` has a `wrong_passphrase` line. Start a request and press Ctrl-C in its terminal: the console says it was withdrawn and discards the next line. Send two requests at once: the second appears only after the first is decided.
9. In a Claude Code session with `claude/skills/envh` and the hook installed: ask it to run a script that needs a key; expect the in-app ask, then the console prompt; deny once and confirm the agent stops and explains.
10. `sudo envh uninstall --dry-run`.
