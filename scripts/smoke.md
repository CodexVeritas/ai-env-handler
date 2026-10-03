# Manual smoke test (not automated: needs root, a terminal and a desktop)

1. `sudo scripts/bootstrap.sh --dry-run` prints the plan; then `sudo scripts/bootstrap.sh`. Choose a passphrase; note the console phrase.
2. Open a separate terminal (not the Claude desktop pane): `sudo envh-console`. Expect: console phrase, passphrase prompt, "console ready".
3. In your own terminal: `envh list` shows no secrets. `cat /var/lib/envh/vault.age` is denied.
4. `envh import --dry-run ~/code/some-project` on a repo with `# Mode` headers; check names and presets; rerun without `--dry-run`, approve on the console, confirm the `.env` diff.
5. `envh session start <preset> --minutes 30 --reason "smoke"`; approve with the code; `envh run --session <id> --reason "smoke" -- env | grep -c KEY`.
6. `envh run --with SOME_SECRET --reason "one-off" -- true`; approve; confirm `audit.jsonl` has request/decision/run lines.
7. Type a wrong code on the console: "no pending request with code".
8. In a Claude Code session with `claude/skills/envh` and the hook installed: ask it to run a script that needs a key; expect the in-app ask, then the console prompt; deny once and confirm the agent stops and explains.
9. `sudo envh uninstall --dry-run`.
