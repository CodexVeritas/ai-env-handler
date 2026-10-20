# Find leftover copies of keys

`envh scan` looks through your home folder for copies of keys outside `.env` files and saves where each one is, never the key. `envh explore` then lets you go through them and replace copies. The setup wizard runs both as its last step.

Keys end up in more places than `.env` files: shell history, Claude Code transcripts under `~/.claude/projects`, notebooks, scratch scripts, other repos, git remotes with embedded tokens, and the local history of VS Code, VSCodium and Cursor (`User/History` under `~/.config/Code`, `~/.config/Code - OSS`, `~/.config/VSCodium`, `~/.config/Cursor`, or `~/.var/app/com.visualstudio.code/config/Code` for Flatpak VS Code), which keeps a copy of a file each time you save it (the setup wizard turns that off for `.env` files, but copies saved before stay). After importing, find them:

```bash
envh scan                      # your home directory
envh scan ~/code ~/Downloads   # specific places; add --json for machine-readable output
envh scan --out ~/Desktop      # save the results somewhere else
```

## What the scan misses

**The scan finds most copies, not all.** A clean scan means none were found where it could look, not that there are none.

- **Databases and other binary files.** SQLite files (where many apps and editors keep their state, such as VS Code's and Cursor's `state.vscdb`) and anything else that looks binary are skipped. `report.txt` lists each one under **Not read**.
- **Git history.** A key that was ever committed stays in `.git/objects`, which is compressed. Search a repo with `git log -p -S<the key's first characters>`.
- **Encrypted stores**, such as the Claude desktop app's local environment, browser profiles' saved logins, and password managers.
- **Keys with no distinctive shape** (FRED, Exa, Mistral and others), unless they sit next to a key-like name such as `FRED_API_KEY=`, or they are in your vault and you allow the scan to search for your vault's keys.
- **Very large files.** Files over 25 MB are skipped, except transcripts and logs, which are read in parts.
- **Anywhere outside the folders scanned**: by default only your home folder. Scan `/tmp` or another drive by naming it (`envh scan /tmp`). Other machines, and online services such as Slack or a hosted AI chat where a key was pasted, are out of its reach.

**If a key may have been copied somewhere the scan can't see, rotate it at the provider.** That is the only fix that covers every copy.

## What a scan records

For every hit it records the file, the line, what kind of key it looks like and how likely it is to be a real key, the length, an id, and the first and last few characters, as the console shows them (fewer for shorter values, none under 12 characters), never the value. The same id means the same value, and its first six characters match the id the console shows for a stored key, so you can tell which vault key a stray copy is. Exit code 1 means something was found. While it runs, a progress bar shows how much it has read and about how long is left.

Each hit says how likely it is to be a real key:
- **Likely key:** the value has a provider's distinctive format, such as `sk-or-v1-...` or `AIza...`, or it is one of your vault's keys, when the scan searched for them.
- **Possible key:** the value is assigned to a key-like name, such as `EXA_API_KEY=...`, `client_secret: ...` or `DB_PASSWORD=...`. The service is taken from the name when it mentions one.
- **Unlikely key:** the value is assigned to some other name holding KEY, TOKEN, SECRET or PASS, such as `cache_key` or `prompt_tokens`. Mostly not keys.
- **Placeholder:** the value looks like a fake or default one from docs or tests: a counting run such as `123456789`, a short unit repeated, or a word such as `example` or `your` (the format's fixed prefix is left out of the check); or it is a database URL with a default or local login, such as `postgres://postgres:postgres@localhost`.

A value that is just a name, such as `api_key = self.settings.openai_api_key` or `OPENAI_API_KEY: OPENAI_API_KEY_PROD`, is code reading a key, so it is not reported. A prefix followed by a long random run, such as `hb_` and 28 hex characters, is a key, not a name. Each hit also gets a place: where it is stored (editor history, trash, envh backups, docker, credential files that apps sign in with, agent transcripts, agent app data, worktree copies) or else what kind of file it is (logs, tests, docs and notes, `.env` files, data and output, code and config, other). Only your home's own `~/.claude`, `~/.cursor` and `~/.codex` count as agent folders; a project's `.claude/worktrees` is a worktree copy.

A home-folder scan can find thousands of hits, so the terminal gets a short summary and the full results go to a new dated folder. At a terminal the scan first asks where; Enter accepts `~/envh-backups-and-scans/scans/` (`--out` chooses without asking):
- `report.txt`: a summary at the top (counts by type and confidence, a table by place, where the keys are), then each distinct value with every file it is in, then every hit by file, then a short recap at the bottom.
- `files.txt`: just the paths of the files with hits, one per line.
- `hits.json`: the same data `--json` prints.

These hold no values, so unlike the import backups they are safe to keep. Later scans skip them.

## Go through the results

**Browse them** with `envh explore` (the newest scan, or `envh explore <folder>`); a scan at a terminal also offers to open it when it finishes. Arrow through each type of key, each key, and the files it is in, grouped by folder when there are many. Enter on a file shows the lines around each copy, read from the file at that moment, with every key in them shown as its first and last few characters; `s` shows the full key until it hides itself after 30 seconds. Tab switches between the Yours, Likely, Possible, Unlikely and Placeholder lists and All, `g` groups by place instead of type (all the editor history copies together, say), and `/` filters by service, name, place or path. The view runs on the terminal's alternate screen, so nothing it shows stays in your scrollback, and it refuses to run without a terminal.

### Start with Yours

Once a scan is checked against your vault, by its search or by the explorer, the explorer opens on **Yours**: the exact copies of your vault's keys, whatever list they are in otherwise. Start there. They are certainly keys, and certainly yours, so they are the safest copies to replace and the ones that matter most. Its top row, **All your keys**, replaces every one of them with one `x`. The other lists hold values that may or may not be keys, and are worth going through after. The answer is saved beside the scan as `vault.json`, names and ids only. The console asks because the answer confirms a guessed value: an agent could check guesses at a short password this way, so approve only a scan you ran.

### Which ones are yours

Before it opens a scan that didn't search your vault, `envh explore` offers to check which found keys are in your vault. The scan's ids (8 characters of each value's SHA-256) go to the broker, and once you approve the match request on the console, it answers with the names of the stored keys among them, never a value. Keys then show "in your vault as NAME", and `v` shows only those, or only the ones that aren't.

## Replace copies

`x` on a row replaces copies with `envh-removed-<type>-<id>`. On a type of key, one key or All your keys, it replaces every copy of those keys, in any list and under any type. On a place such as editor history, it replaces every copy in that place, and on a folder or file, the copies there. Filters you have on still apply. A summary first says how many copies, keys and files by place, and which other keys of the same type stay (in Yours, ones that aren't in your vault, such as an old key). Only `y` goes ahead. The files are read again and rewritten in place, byte for byte apart from the keys, keeping their permissions, with no backup, since a backup would be one more copy. A file changed in the last 10 minutes is skipped, since a program such as an agent may still be writing it; so are links. Private keys are left for you, because only their first line is matched. Files in git repos get a warning: a committed key stays in the repo's history, so rotate it. What was replaced is logged beside the scan in `replaced.txt`, paths and ids only.

## Copies of your own keys

At a terminal, the scan first offers to search for copies of the keys in your vault, which finds them even where nothing marks them as keys, such as a bare paste in a transcript or `curl -H 'X-Api-Key: ...'`. You approve the search once on the console. The broker then shares each stored key's first and last few characters and its length, as the console shows them, never the key; keys under 20 characters are left out, since their ends would match too much. The scan looks for places that start and end that way at that length. When it is done, it asks the broker once, on the same connection, which of all the values it found are stored keys. Those count as Likely keys, the answer is saved beside the scan as `vault.json`, and a copy found only this way is replaced where the scan saw it, if the same value is still there. If you say no, or the console isn't running, the scan works as before.

## What it skips on purpose

It skips only what can't hold your keys or is cleared on its own, and `report.txt` lists everything it left out under **Not read**: each file over the size limit, each file that looks binary, each skipped folder by rule, and each link to a folder outside the paths scanned. The summary gives the counts.
- **Large files:** files over 25 MB (`--max-size`) are left out, except transcripts and logs, which are read in parts at any size. The explorer shows and replaces files up to 256 MB.
- **Installed code:** `node_modules`, `site-packages`, a Python or conda environment's `lib`, `share` and `include` (its `bin` and `etc` are read, since activate scripts can export keys), and editor extensions.
- **Tools' download caches,** such as `.cache/pip`, `.cache/uv`, `.npm/_cacache`, `.cargo/registry`, `.m2/repository` and `.gradle/caches`. The login and config files next to them are read: `.cargo/credentials.toml`, `.m2/settings.xml`, `.gem/credentials`, `.local/share/uv/credentials`, `.terraform/terraform.tfstate` and the like.
- **Caches apps clear as they go:** `Cache`, `Code Cache`, `GPUCache` and the like, a Flatpak app's `cache` folder, Next.js's `.next/cache` and `.next/dev`, and folders tagged with `CACHEDIR.TAG`.
- **Inside `.git`:** only `objects` and `lfs`, which are compressed. Its config, hooks and logs are read.
- **Too big, and none of your keys:** Steam's games, Flatpak's runtimes and Wine's Windows folder.
- **Browser and mail profiles** (Firefox, Chrome, Chromium, Brave, Edge, Thunderbird). Their saved passwords and logins are encrypted and can only be cleared from inside each app, so the summary reminds you to do that.

`envh scan --help` lists every skip rule.

## Keys it recognizes

- **By value, anywhere:** OpenAI, OpenRouter, Anthropic, xAI, DeepSeek (and other `sk-` keys of 32 hex characters), Perplexity, Google (API keys, OAuth client secrets and access tokens, base64 service-account files), Groq, Together, Fireworks, Cerebras, Hugging Face, Replicate, ElevenLabs, E2B, Tavily, Firecrawl, LangSmith, Pinecone, Supabase, GitHub, GitLab, Slack (tokens and webhooks), Discord webhooks, Twilio, SendGrid, Stripe (keys and webhook secrets), Shopify, Square, AWS (access keys, Bedrock keys), Cloudflare origin CA keys, DigitalOcean, Heroku, Fly.io, PlanetScale, Databricks, Doppler, HashiCorp Vault, Grafana, Sentry (tokens and DSNs), New Relic, PostHog, Linear, Atlassian, Airtable, Notion, Postman, npm, PyPI, 1Password, age, Resend, Browserbase, JWTs, private keys, database URLs with passwords. Where gitleaks has a rule for a provider, the format follows it.
- **Only next to a key-like name** (`FRED_API_KEY=...`, `serp_api_key: "..."`, `PASSWORD=...`, `--api-key ...`, `Authorization: Bearer ...`): SerpAPI, FRED, AskNews, Hyperbrowser, Metaculus, Exa, Deepgram, Parallel, Mistral, Cohere, Telegram, Mailchimp, Azure, AWS secret access keys and other keys that are plain hex or random strings. A bare copy of one of these in a transcript has no distinctive shape, so it is found only when it is in your vault and the scan searches for your vault's keys (see [Copies of your own keys](#copies-of-your-own-keys)).

The scan stays separate from the import wizard on purpose: the wizard moves values out of files you chose, the scan is a read-only audit of everything else. The import prints a reminder to run it.
