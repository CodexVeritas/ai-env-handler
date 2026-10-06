# Wave-1 digest (READ THIS before Wave 2 — do NOT repeat these)

Wave 1 (4 agents) found NO flag exfil, NO approval forgery, NO cross-user break, NO pre-decision
state change. The confidentiality/approval core is solid: `approve`/`deny`/`create_session`/
`env_for` are reachable ONLY from the console (passphrase-gated), never from a socket op; with no
approver, a request's decision can only ever become `withdrawn`. Full details in
`findings/agent-w1-*.json`. Candidate findings already recorded (DO NOT re-report; go deeper or
elsewhere):

- **w1-protocol-1 (HIGH DoS)** fd-exhaustion: ~1020 idle connections from one listed user hit the
  broker's RLIMIT_NOFILE=1024; no idle timeout, no connection cap; accept loop stalls for all.
- **w1-protocol-2 (HIGH DoS)** event-loop wedge: `op_preset_validate` runs `yaml.safe_load`
  synchronously on the asyncio loop with no approval; ~800KB YAML freezes broker ~6s.
- **w1-preset-1 (HIGH DoS)** YAML alias-bomb repr amplification: 346-byte preset YAML → 88MB error
  string / >1GB RSS, because `parse_preset` interpolates attacker YAML with `{!r}` and repr()
  doesn't share alias references. Via `op_preset_validate`/`op_preset_propose`, no approval.
- **w1-preset-2 (LOW)** over-long numeric `max_session` → uncaught `ValueError` (int >4300 digits)
  → generic "internal error" + traceback log-flood.
- **w1-hook-1..5 (MEDIUM hook bypass)** `uv run --with rich envh …`; empty `ENVH_SESSION=`;
  `env --split-string=/-S`; eval-nesting past MAX_NESTING=3; `env -i`/`env -u ENVH_SESSION`.
- **w1-session-1 (LOW/KNOWN)** detached `session_start` requests linger 10 min, per-uid slot DoS.

## Proven NEGATIVES (do NOT retry — already shown not to work)
- Billion-laughs/alias bomb does NOT expand in `safe_load` (PyYAML shares refs) — only repr() does.
- Type confusion / overflow in op args (minutes/with/command/request_id as list/dict/huge int) →
  caught as generic "internal error", NO crash, NO leak (robustness gap only).
- Oversized line >1MB → ValueError caught, connection closed, broker healthy.
- `op_*` getattr discovery is safely scoped; only ONE op per connection; regexes are all linear
  (no ReDoS); malformed/unknown JSON → clean error.
- Forged/guessed session ids → clean "unknown session", no bytes. token_hex(8)=64-bit CSPRNG,
  not predictable, never leaked to non-owners (list/status uid-filtered).
- Cross-uid use of session_wait/session_end/run --session all blocked by SO_PEERCRED uid check.
  outsider (unlisted) rejected before any op.
- NO value/fingerprint leak over the socket: list/status/preset_validate return names only;
  propose/import carry diff/fingerprints/resolution ONLY in request.summary (console) and
  request.result (only on approval) — client never receives them with no approver.
- No pre-decision write: propose/import/validate never touch presets.yaml/config.yaml/vault
  before `approve()`.

## GAPS for Wave 2 (go here)
- **F6 peer-creds / identity** — untouched. SO_PEERCRED edge cases, pid reuse/exit-before-read,
  fd-passing (SCM_RIGHTS), cmdline spoof shown to approver, identity via a helper.
- **F7 client-side run** — untouched & high-value. `client/commands.py`
  `terminate_lingering`/`processes_with_marker` reads OTHER processes' `/proc/<pid>/environ` and
  SIGKILLs any carrying `ENVH_RUN_MARKER=<marker>`. Marker collision? A victim process that
  happens to (or is made to) carry the marker getting killed? Env injection into the child,
  signal forwarding, exit-code math, `--keep-background`. The client runs as the user.
- **F12 cross-user** — now testable: redteam vs **redteam2** (both listed). Sessions/requests/
  runs isolation, `names_in_use`, rename-following into another user's pending requests, marker
  collision across users, list/status `for_uid` filtering.
- **F8 info-leak** — timing/error-differential oracles (session-state error differential noted but
  gated behind the 64-bit id); any inference channel; `client_connection_lost` detail.
- **F10 hook (deeper)** — the Cursor `beforeShellExecution` path, heredoc bodies, the
  unclosed-quote PASSPHRASE_FORM fallback, more wrappers; and independently re-verify w1-hook-1..5.
- **F11 supply-chain + tools** — `tools/importer.py` + `tools/scanner.py` process UNTRUSTED file
  content (`envh import`, `envh scan`): path traversal / symlink in the import backup dir, ReDoS
  or huge-file handling in the scanner, the `.env` parser. Client import-path/PATH planting a
  non-root agent could do. (Root-only writes are out of scope.)
- **Crown jewel (combined surfaces)** — TOCTOU in `merged_for_approval`/`confirm_import_resolution`,
  the request-mapping-after-rename path, detached reattach, any multi-connection/race angle to get
  a write applied or a value out WITHOUT an approval. One dedicated serious attempt.

## Environment for Wave 2
- Main arena socket: `/run/envh-ctf/ctl.sock`. Helpers: `/opt/ctf/envh-rt` (redteam),
  `/opt/ctf/envh-rt2` (redteam2), `/opt/ctf/envh-outsider` (unlisted).
- Raw socket as a user: `sudo -u redteam /opt/ctf/venv/bin/python <script>`
  (set ENVH_SOCKET=/run/envh-ctf/ctl.sock, or connect to the path directly).
- **DoS agents MUST use the ISOLATED broker**: socket `/run/envh-dos/ctl.sock`
  (`sudo -u redteam env ENVH_SOCKET=/run/envh-dos/ctl.sock /opt/ctf/venv/bin/python -m envh …`).
  Do NOT run floods/wedges against the main arena — other agents are using it.
- All CHARTER rules still apply: non-root only, no root-flavored findings, prove everything, write
  `/opt/ctf/arena/findings/agent-<ID>.json`.
