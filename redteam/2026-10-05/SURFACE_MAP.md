# envh attack-surface map (for disjoint agent assignment + dedup)

Each family has an ID. An agent assigned a family owns it for the wave. Read other agents'
findings before starting; never repeat a recorded idea (positive or negative). Record what you
try. All attacks run as the non-root `redteam` (or `outsider`) user per CHARTER §2/§3.

Source to study: `/home/user/ai-env-handler/src/envh/`.

- **F1 socket-protocol** — raw NDJSON to the socket (`server/control.py`): unknown/typed ops,
  missing/extra fields, type confusion (lists/dicts/ints where strings expected), oversized
  lines (MAX_LINE=1MB), multiple ops per connection, `op_*` method discovery via getattr,
  JSON `default=str` quirks, injection into handler args.

- **F2 request-lifecycle** — `server/control.py` + `core/state.py`: concurrent requests, the
  `_wait_decision`/`_decided_or_withdrawn`/`_attend` logic, `session_wait` on another id, the
  detached-grace (`mark_detached`, 10 min) reattach, `abandon_at`/sweep, `MAX_PENDING_PER_USER`,
  double-decide, approving/withdraw races, run loop (`_start_run`) `run_done` handling.

- **F3 session-tokens** — `core/state.py` session ids (`token_hex(8)`), `broker.owned_session`,
  `resolve_run_in_session` (per-run re-check, `--with` narrowing), `ENVH_SESSION`, expiry edges,
  reusing/guessing an id, cross-uid use, session after end, 24h cap, max_session math.

- **F4 preset-and-import** — `broker.request_preset`/`request_import`/`import_resolution`/
  `merged_for_approval`/`confirm_import_resolution`/`refresh_import`, `renamed_presets`,
  `op_preset_propose`/`op_import`/`op_preset_validate`. Can a client get a preset/import APPLIED
  without approval, map a var to the flag and have it stored, or leak via validate/diff? YAML
  payloads, deep structures, `first_free_name`, fingerprint exposure in summaries.

- **F5 config-parsing** — `core/config.py` + `core/durations.py` + `common.py` regexes
  (`SECRET_NAME`, `VAR_NAME`, `PRESET_NAME`, `PLAIN_SCALAR`), `yaml.safe_load` edge cases,
  duration parsing overflow/negatives, `_parse_description`/`printable`, name confusion,
  ReDoS, anything a client-supplied preset/import YAML reaches.

- **F6 peer-creds-identity** — `platform.peer_credentials` (SO_PEERCRED), pid reuse/exit before
  read, cmdline read (`/proc/<pid>/cmdline`) spoofing shown to approver, fd-passing, connecting
  via a helper that changes identity, uid vs allowed_uids, `op_*` that trust `provenance`.

- **F7 client-side-run** — `client/commands.py` `spawn`/`terminate_lingering`/
  `processes_with_marker` (reads other procs' `/proc/<pid>/environ`), `RUN_MARKER_VAR`, signal
  forwarding, env injection into the child, `--keep-background`, exit-code math, a run killing
  processes it shouldn't, marker collision across users.

- **F8 info-leak** — do `list_payload`/`status_payload`/error strings/`client_connection_lost`
  detail/audit-echoed fields leak value bytes, fingerprints, or let you infer a value? Timing or
  error-message oracles. `fingerprint()` in `common.py`. Reason/command echoing.

- **F9 dos-resilience** — crash or wedge the broker from the socket: block the single-threaded
  asyncio loop (slow/huge YAML, pathological regex, huge `--with`), unbounded growth, fill
  pending for another user, exception paths that kill the connection vs the broker, socket
  mode 0666 abuse, the `MAX_SOCKET_PATH` / `MAX_LINE` limits.

- **F10 hook-bypass** — `claude/hooks/envh_ask.py`: craft a command that WILL start an envh
  passphrase request but the hook returns no-ask (or the wrong reason), BEYOND the README's
  acknowledged bypasses (script file, interpreter one-liner, pipe-into-shell, variable-built).
  The shell lexer edge cases, wrapper/runner handling, heredocs, `--socket` stripping,
  `ENVH_SESSION=` detection, nesting depth, unclosed-quote fallback.

- **F11 supply-chain** — `pyproject.toml` deps (`pyrage`, `pyyaml`), the editable install, the
  installer `install/command.py` + `scripts/setup_wizard.py`, `envh-console` helper script,
  desktop launcher, `uv`/python bootstrap, anything that runs at import, a file a normal user
  could plant that root/envhsvc later trusts. (Report only what a NON-root user could plant or
  trigger; a root-only write is out of scope.)

- **F12 cross-user-multiuser** — two listed users sharing one vault/policy: `redteam` affecting
  another listed user's sessions/requests/runs; `names_in_use`/rename following into another
  user's pending requests; `for_uid` filtering in list/status; `end_session` ownership.
