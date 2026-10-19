# Move your .env files over

## Move your `.env` files over

```bash
envh import --dry-run ~/code            # scan a directory (depth 3) and show the plan
envh import ~/code                      # or give specific files: envh import ~/code/bot/.env
```

Import one project now and others whenever you like; an import never overwrites what is stored. Keys are named `<PROJECT>_<NAME>`, where the project is the folder that holds the `.env` (`news-bot/.env` gives `NEWS_BOT_OPENAI_API_KEY`). A key whose value is already in the vault is reused under its stored name instead of being stored twice. If a name is already taken by a different value, the new one gets a number (`NEWS_BOT_OPENAI_API_KEY_2`). The console shows the final names before you approve, and the presets and the rewritten `.env` comments use them. To rename a key, describe it or replace a stored value later, use `/keys` on the console (see [The console](console.md#the-console)).

The wizard walks through six steps and writes nothing until the last one:

1. **Files.** Lists every `.env` and `.env.*` found (not `.env.example`); choose all or some.
2. **Contents.** Parses active lines and commented-out assignments, and treats comment headers as **groups**. A comment holding a long token that is not a word, such as a key, is never taken for a header, so it is not printed or built into names; it stays in the file, and `envh scan` reports it. A file like this

   ```
   GITHUB_TOKEN=...
   LOG_LEVEL=info

   # Team Mode
   OPENROUTER_API_KEY=...
   ASKNEWS_API_KEY=...

   # Personal Mode
   # OPENROUTER_API_KEY=...
   # ASKNEWS_API_KEY=...
   ```

   has a base group (`GITHUB_TOKEN`, `LOG_LEVEL`) and two mode groups. Each variable is classified as secret or config by its name and value; you can flip any.
3. **Names.** Secrets get vault names that start with the project: `NEWS_BOT_GITHUB_TOKEN`, `NEWS_BOT_TEAM_OPENROUTER_API_KEY`, `NEWS_BOT_PERSONAL_OPENROUTER_API_KEY`, and so on. Identical values share one entry, across projects and with keys already in the vault. Rename anything.
4. **Presets.** One preset per group, named `<repo>-<group>` (`news-bot-team`, `news-bot-personal`), each containing the base variables plus the group's. A repo without groups gets one preset named after it. Drop presets, then rename them, both by the names listed.
5. **Plan.** Secrets to store (names and fingerprints only), presets in full, and per file the lines that change, shown as they will read: each secret line becomes `# VAR -> envh secret NAME (presets: ...)`. The old lines are not shown, since they hold the values. Headers and config lines stay as they are. Choose per file whether to rewrite it.
6. **Apply.** The console shows the same summary and asks for your vault passphrase. Then each original file is backed up and the files are rewritten.

**The backup.** Before rewriting, the wizard asks where to keep the backup; Enter accepts `~/envh-backups-and-scans/import-backups/`, a folder in your home that is easy to find again. Each original file is copied into a new dated folder there, at its full path: `/home/you/code/bot/.env` is backed up as `.../<date-time>/home/you/code/bot/.env`. The folder is mode 0700 and the wizard prints its exact path, with the command to delete it. The copies hold the old values in plaintext, readable by anything running as you, just as the original files were. So once the rewritten files work, delete the folder (`rm -r ~/envh-backups-and-scans/import-backups/<date-time>`). `envh scan ~` keeps reporting it until you do. A `README.txt` in `~/envh-backups-and-scans` says what each subfolder holds.

To see a value later without the backup: `envh run --with VAR=SECRET --reason "recover" -- printenv VAR`, approved on the console.
