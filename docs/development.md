# Development

## Set up and test

```bash
uv sync --all-groups
uv run pytest -q                       # no root, no network, no environment variables needed
uv run envh --help
```

**Test the install on other distributions** with Docker or Podman. `scripts/ci/install_matrix.sh` sets up a fresh container the way a desktop install has it, then runs the setup wizard end to end: install, console, a client, the Claude Code hook, closing the console's window, uninstall. CI runs it on ten distributions (`.github/workflows/install-matrix.yml`). Containers share your kernel, so this tests each distribution's userspace, not its kernel settings:

```bash
docker run --rm -v "$PWD:/src:ro" ubuntu:26.04 sh /src/scripts/ci/install_matrix.sh
```

## Record the demo video

The walkthrough video at the top of the README is a real recording of a development broker with fake keys. After the console changes, record it again and render it (needs ffmpeg, Google Chrome, and network access to load xterm.js from jsDelivr). `--small` writes a copy under GitHub's 10 MB limit for videos:

```bash
uv run python scripts/demo/record.py build/demo/recording.json
uv run --with playwright python scripts/demo/render.py build/demo/recording.json --mp4 build/demo/envh-demo.mp4 --small docs/demo.mp4
```

Then drag `docs/demo.mp4` into a comment box on GitHub, without posting it, and put the link GitHub gives it at the top of `README.md` in place of the old one.

## Run a development broker

Run a development broker as yourself, without installing: `uv run envh init --data-dir /tmp/envh-dev`, then in a terminal `uv run envh serve --data-dir /tmp/envh-dev --socket /tmp/envh-dev/ctl.sock`, and point clients at it with `--socket /tmp/envh-dev/ctl.sock` or `ENVH_SOCKET=/tmp/envh-dev/ctl.sock`. This gives none of the user-separation guarantees; it is for working on envh itself.

## Code layout

Layout, in review order. The directory tree states the trust boundaries and a test (`tests/test_boundaries.py`) keeps the imports inside them:

| Package | Side of the boundary | Contents |
|---|---|---|
| `src/envh/core/` | trusted logic, no sockets or terminals | `config.py` policy files, `durations.py`, `vault.py` (age encryption), `state.py` (requests, sessions, runs), `broker.py` (decisions and their side effects), `audit.py` |
| `src/envh/server/` | the trusted process, runs as user `envh` | `control.py` (Unix-socket protocol, peer uid), `console/` (the full-screen console: `app.py`, the request card, the keys, presets, settings and sessions screens, and `tui.py`, its standard-library terminal toolkit), `hardening.py`, `serve.py` (startup), `init_cmd.py` |
| `src/envh/client/` | the untrusted side, runs as you or an agent, standard library only | `transport.py` (socket client), `commands.py` (`envh run`, `session`, `list`, …) |
| `src/envh/tools/` | set-aside utilities, standard library only | `importer.py` (the `.env` wizard), `scanner.py` (`envh scan`), `explorer.py` (`envh explore`), `replacer.py` (its bulk replace), `vault_search.py` (the scan's search for your vault's keys), `saved_files.py` (the folder they save into); deleting the package removes three subcommands and nothing else |
| `src/envh/install/` | root-only system setup | `command.py` (`envh install` / `uninstall`, the console helper and launcher) |
| `src/envh/platform.py`, `common.py`, `cli.py` | shared | OS facts (uid, socket path, prctl, preflight), the value fingerprint, command dispatch |

The security argument lives in `core/` and `server/`. In the console, `app.py` and `request_card.py` decide what a typed passphrase can do and what a request shows; the other screens change things only through the broker, after the passphrase. The rest cannot touch a secret except through the socket like any other client.
