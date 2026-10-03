# Manual smoke test (not automated: needs root, a terminal and a desktop)

1. `python3 scripts/setup_wizard.py`: the dry run prints the plan; answer n once to check that nothing changed (no `/opt/envh`, no `envh` user), then rerun and install. Choose a vault passphrase and an approval password; note the console phrase. `ls -l /usr/local/bin/envh` points into `/opt/envh/env`, and `/opt/envh/installed-from` names the commit.
2. Open a separate terminal (not the Claude desktop pane): `sudo envh-console`. Expect: console phrase, passphrase prompt, "console ready".
3. In your own terminal: `envh list` shows no secrets. `cat /var/lib/envh/vault.age` is denied.
4. `envh import --dry-run ~/code/some-project` on a repo with `# Mode` headers; check names and presets; rerun without `--dry-run`, approve on the console, confirm the `.env` diff, check the original is in the printed backup folder, then delete that folder.
5. `envh session start <preset> --minutes 30 --reason "smoke"`; approve with the code and the approval password; `envh run --session <id> --reason "smoke" -- env | grep -c KEY`.
6. `envh run --with SOME_SECRET --reason "one-off" -- true`; approve; confirm `audit.jsonl` has request/decision/run lines.
7. Type a wrong code on the console: "no pending request with code". Type a right code with a wrong password: "wrong approval password", the request stays pending, and `audit.jsonl` has a `wrong_approval_password` line.
8. In a Claude Code session with `claude/skills/envh` and the hook installed: ask it to run a script that needs a key; expect the in-app ask, then the console prompt; deny once and confirm the agent stops and explains.
9. `sudo envh uninstall --dry-run`.
