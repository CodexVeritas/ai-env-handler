# envh attack-surface map v2 (for disjoint agent assignment + dedup)

Each family has an ID and a persona. An agent assigned a family owns it for the wave. Read all
existing `evidence/findings/*.json` before starting; never repeat a recorded idea (positive OR
negative). Record what you try. All attacks run only as `rt1`/`rt2`/`outsider` per CHARTER §2/§3.

Source to study: `/home/user/ai-env-handler/src/envh/`. Taxonomy tags map each family to recognized
classes (see `standards/TAXONOMY_MAP.md`): ATT&CK = MITRE ATT&CK, ATLAS = MITRE ATLAS,
LLM = OWASP Top 10 for LLM Apps 2025, ASVS = OWASP ASVS v5.0.

Families F1–F12 are inherited from the 2026-10-05 pass (the prior map); F13–F16 and the regression
surface are new this pass. Prior confirmed issues on each family are noted so agents go beyond them.

- **F1 socket-protocol** (`server/control.py`) — raw NDJSON: unknown/typed ops, missing/extra
  fields, type confusion, oversized lines (`MAX_LINE`), multiple ops per connection, `op_*`
  discovery via `getattr`, `json.dumps(default=str)` quirks, injection into handler args.
  *Prior: fd-exhaustion (V3), event-loop wedge (V2) — fixed; go beyond.* Tags: ATT&CK T1190, ASVS V13.

- **F2 request-lifecycle** (`control.py` + `core/state.py`) — concurrent requests,
  `_wait_decision`/`_decided_or_withdrawn`/`_attend`, `session_wait` on another id, detached-grace
  reattach, `abandon_at`/sweep, `MAX_PENDING_PER_USER`, double-decide, approve/withdraw races, the
  run loop `run_done`. Tags: ATT&CK T1499, ASVS V11.

- **F3 session-tokens** (`core/state.py`) — session ids (`token_hex`? adjective-noun), `owned_session`,
  `resolve_run_in_session` (per-run re-check, `--with` narrowing), expiry edges, guessing/reusing an
  id, cross-uid use, session after end, 24h cap, max_session math. Tags: ATT&CK T1528/T1552, ASVS V3/V7.

- **F4 preset-and-import** (`broker.request_preset`/`request_import`/`import_resolution`/
  `merged_for_approval`/`confirm_import_resolution`/`refresh_import`, `op_preset_propose`/`op_import`/
  `op_preset_validate`) — get a preset/import APPLIED without approval, map a var to the flag and have
  it stored, or leak via validate/diff; YAML payloads, fingerprint exposure. *Prior: import equality
  oracle (V6), oversized/alias YAML (V1/V2) — fixed; find a NEW oracle or bypass.* Tags: ATLAS AML.T0024, LLM02/LLM05, ASVS V1/V5.

- **F5 config-parsing** (`core/config.py` + `core/durations.py` + `common.py` regexes) —
  `yaml.safe_load` edge cases, duration overflow/negatives (V5 fixed), `_parse_description`/
  `printable`, name confusion, ReDoS, anything a client-supplied preset/import YAML reaches. Tags:
  ATT&CK T1499.004, ASVS V5.

- **F6 peer-creds-identity** (`platform.peer_credentials` SO_PEERCRED) — pid reuse / exit-before-read,
  cmdline spoof shown to approver (acknowledged), fd-passing (SCM_RIGHTS), identity-changing helpers,
  uid vs allowed_uids, privilege-drop-before-`listen` SO_PEERCRED timing, `op_*` that trust
  provenance. Tags: ATT&CK T1134, ASVS V3/V13.

- **F7 client-side-run** (`client/commands.py` `spawn`/`terminate_lingering`/`processes_with_marker`)
  — reads other procs' `/proc/<pid>/environ`, `ENVH_RUN_MARKER`, signal forwarding, env injection
  into the child, `--keep-background`, exit-code math, a run killing processes it shouldn't, marker
  collision across users. Tags: ATT&CK T1055/T1057, ASVS V13.

- **F8 info-leak** — do `list_payload`/`status_payload`/error strings/`client_connection_lost`
  detail/audit-echoed fields leak value bytes, fingerprints, or let you infer a value? Timing or
  error-message oracles; `fingerprint()`/`value_id()`/`peek_ends()` in `common.py`; reason/command
  echoing. *Prior: import equality oracle (V6) — fixed; find a new oracle.* Tags: LLM02, ATLAS AML.T0024, ASVS V8.

- **F9 dos-resilience** — crash/wedge the broker from the socket: block the single-thread asyncio
  loop (slow/huge YAML, pathological regex, huge `--with`), unbounded growth, fill pending for another
  user, exception paths that kill the connection vs the broker, socket-mode abuse, `MAX_SOCKET_PATH`/
  `MAX_LINE`. *Prior: V1/V2/V3/V4 — fixed; find a new exhaustion/wedge.* **Use your own instance.**
  Tags: ATT&CK T1499, ASVS V11.

- **F10 hook-bypass** (`claude/hooks/envh_ask.py`) — a command that WILL start an envh
  passphrase-requiring request but the hook returns no-ask (or the wrong reason), BEYOND the README's
  acknowledged bypasses and the V8 fixes: shell-lexer edge cases, wrapper/runner handling, heredocs,
  `--socket` stripping, `ENVH_SESSION=` detection, nesting depth, unclosed-quote fallback; both Claude
  Code and Cursor input shapes. Tags: LLM01 (prompt injection), ATLAS AML.T0051, ASVS V5.

- **F11 supply-chain** (`pyproject.toml` deps, editable install, `install/command.py`,
  `scripts/setup_wizard.py`, `envh-console` helper, desktop launcher, uv/python bootstrap) — anything
  that runs at import, or a file a NON-root user could plant that root/service-user later trusts. A
  root-only write is out of scope. Tags: ATT&CK T1195, LLM03, ASVS V10.

- **F12 cross-user-multiuser** — two listed users sharing one vault/policy: `rt1` affecting `rt2`'s
  sessions/requests/runs; `names_in_use`/rename following into another user's pending requests;
  `for_uid` filtering in list/status; `end_session` ownership. *Prior: `_next_id` count leak (V7) —
  fixed; find a new cross-user effect.* Tags: ATT&CK T1078, ASVS V3/V8.

### New this pass

- **F13 excessive-agency / confused-deputy** (AI-agent lens; CHARTER §4a) — can an agent with
  legitimate access be steered into surfacing/widening access beyond the human's intent via a crafted
  `--reason`, a preset proposal, bait-and-switch, approval-fatigue, or chained flows, in a way that
  beats the README's "approve a command line, not the code" acknowledgement (i.e. a real mis-parse or
  an unapproved policy/preset write)? Tags: LLM06 (excessive agency), LLM08, ATLAS AML.T0053.

- **F14 detection / defense-in-depth (blue team)** — does envh *detect, log, throttle, and
  fail-closed* under attack? Audit completeness & integrity (does an attack leave the right events?
  can a client suppress/forge/flood audit lines? — note V4 capped `reason`), the unknown-session-id
  lockout (`MAX_UNKNOWN_SESSION_IDS`), connection caps, ENOSPC behavior. A gap where a real attack
  leaves NO trace is a finding. Tags: ASVS V7 (logging), ATT&CK T1562.

- **F15 host-config / hardening-gate matrix** (Tier 2 VM / Tier 3 CI — NOT runnable in this
  container) — the negative/assurance tests of the degraded setups the README documents:
  `legacy_tiocsti=1`, Yama absent/`0`, writable `0666 /dev/uinput`, X11 vs Wayland, tmux/screen,
  `NOPASSWD` sudo, user-in-`docker`-group; and the positive gates `legacy_tiocsti=0`, Yama
  `ptrace_scope>=1`. Each must assert the guarantee **fails closed and is reported**, never silently
  lost. See `tiers/`. Tags: ASVS V1/V14, NIST SP 800-53 SI-6.

- **F16 automated fuzz sweep** (large-N, mechanical) — stochastic mutation over the NDJSON protocol,
  the hook bash-lexer, preset/import YAML, and duration strings, run as `rt1` with timeouts; triage
  crashes/hangs/unexpected replies. Complements the reasoning agents (attack success scales with
  samples). Tags: ATT&CK T1190, ASVS V13.

- **Regression surface** — re-attack the V1–V8 fixes for *bypasses* (not re-derivations): the import
  oracle (V6), the DoS caps (V1–V4), duration bounds (V5), per-user ids (V7), the hook parser (V8).
  A working bypass is a regression finding (HIGH if it reopens a HIGH).
