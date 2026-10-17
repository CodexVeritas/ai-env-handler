# envh red-team — findings

Structured, pre-committed red-team of `envh`. Scope, win conditions, and the grading rubric were
frozen in `CHARTER.md` (SHA-256 in `PRECOMMIT.txt`) **before** any attack agent ran. A headless
broker ran as a dedicated service user with **no approver attached**; attack and grading agents
operated **only as the non-root listed user** `redteam` (and `redteam2`/`outsider`). Every finding
below was **independently reproduced by a separate grading agent running as non-root** and checked
against `README.md` + `SECURITY.md` for novelty. Root-only attacks (reading the vault, broker
memory, ptrace) were out of scope by instruction and none are reported.

## Main point

- **The crown jewel held.** Across 12 attack agents over two waves, **no one extracted the secret
  `CAPTURE_THE_FLAG` (or any secret value), forged or bypassed an approval, wrote policy/presets/
  the vault without a console approval, broke cross-user session isolation, or spoofed the
  `SO_PEERCRED` identity.** The value-bearing path is gated behind console approval and that gate is
  sound. This is the most important result.
- **The gaps are availability and a confidentiality side channel, not a direct break.** The real,
  novel issues are: a single non-root listed user can **crash or wedge the whole broker** several
  ways (DoS); an **`op_import` equality oracle** lets a listed user confirm a *guessed* secret value
  pre-approval without an audit trail; a **cross-user activity-count leak**; and **10 ways to slip a
  passphrase-requiring command past the in-app hook** (which the README already says is not a
  boundary).
- **17 findings graded CONFIRMED-novel** (3 HIGH, 1 MEDIUM-DoS, 11 MEDIUM, 2 LOW-hook, 1 LOW-leak…
  see table), **1 KNOWN-but-real**, **2 REJECTED** as useless/out-of-scope. None are "root-flavored."

## Confirmed-novel vulnerabilities

| ID | Severity | Class | One-line |
|----|----------|-------|----------|
| V1 | **HIGH** | DoS (crash) | Alias-shared YAML `repr()` in preset-validation errors → event-loop wedge **and full OOM crash** of the broker, from a ~350-byte unapproved request |
| V2 | **HIGH** | DoS (wedge) | `yaml.safe_load` + validation run synchronously on the single asyncio loop; one ~800 KB unapproved request freezes the whole broker (and console) for seconds; sustain to freeze indefinitely |
| V3 | **HIGH** | DoS (exhaustion) | No idle timeout / no connection cap: ~1020 idle connections from one listed user pin the broker to its fd ceiling and stop it serving everyone |
| V4 | MEDIUM | DoS (disk) | The requester-controlled `reason` is written to the audit log **uncapped** (console only truncates the display); ~1 MB/run → audit bloat / disk-fill → all broker writes fail on ENOSPC |
| V5 | LOW | robustness | Over-long numeric `max_session` raises an uncaught `ValueError`/`OverflowError` → misleading "internal error" + a broker traceback per request (log-flood) |
| V6 | MEDIUM | info-leak | **`op_import` is a pre-approval exact-value equality oracle** on any named secret; reply flips ERROR-vs-PENDING by whether a guessed value matches the stored one — and a correct guess writes **no audit event** |
| V7 | LOW | info-leak | Global `_next_id` counter (shared by requests+runs, printed as `request #N`) leaks another listed user's activity **count** across the per-user boundary |
| V8 | MEDIUM/LOW | hook bypass | **10 distinct ways** to run a passphrase-requiring `envh` command while the Claude Code/Cursor hook stays silent, beyond the 4 bypass classes the README acknowledges |

### V1 — Unbounded `repr()` of alias-shared YAML → broker wedge + OOM crash  (HIGH, CONFIRMED)
- **Where:** `src/envh/core/config.py` `parse_preset` interpolates attacker-controlled YAML nodes into
  validation errors with `{!r}` (the `max_session`, per-var `approval`, and `secret` fields).
  Reached, with **no approval**, via `op_preset_validate` / `op_preset_propose`
  (`src/envh/server/control.py`).
- **Mechanism:** PyYAML keeps YAML aliases as *shared references* (a small DAG), so `yaml.safe_load`
  of an alias bomb is fast — but `repr()` does **not** share references, so repr of a `width**depth`
  alias graph materialises an astronomically large string. `RLIMIT_AS` is unlimited (hardening only
  zeroes `RLIMIT_CORE`), so nothing bounds the allocation but the OOM killer, which kills the broker.
- **Proof (non-root `redteam`, isolated broker):** a 489-byte `preset validate` drove broker RSS from
  ~5.6 GB to ~14 GB and was OOM-killed (`dmesg: Killed process … (python) anon-rss:13938768kB`);
  afterward the socket was dead (`ECONNREFUSED`). A 346-byte payload returns an **88.7 MB** error for
  346 bytes of input (~260,000×). Reproduced by both the finder and the DoS grader.
- **Fix:** never `repr()` parsed values in error messages (use a type name / truncated `str`); cap
  parse node-count / input complexity; set `RLIMIT_AS`.

### V2 — Synchronous YAML parse on the event loop → whole-broker wedge  (HIGH, CONFIRMED)
- **Where:** `op_preset_validate` → `validate_preset_yaml` → `parse_presets` → `yaml.safe_load`
  runs **synchronously on the single asyncio loop**, with no `run_in_executor` and no size budget,
  and **needs no approval**. `op_preset_propose`/`op_import` reach the same path (and are ~2.3× heavier
  via deepcopy+dump+difflib).
- **Proof:** one ~800 KB `preset validate` delayed a concurrent `list` from 0.003 s to **8.44 s**;
  back-to-back/concurrent calls keep the broker (and the approval console) frozen indefinitely.
- **Fix:** offload parsing to a thread/executor; cap input size before parsing; validate cheaply first.

### V3 — Connection fd-exhaustion (no idle timeout, no connection cap)  (HIGH, CONFIRMED)
- **Where:** `src/envh/server/control.py` `ControlServer`/`Connection.serve` — after the uid check,
  the handler blocks in `await reader.readline()` with **no idle timeout**, there is **no per-uid or
  global connection cap**, and `RLIMIT_NOFILE` is left at the default (1024).
- **Proof:** one `redteam` process opened 1016 idle connections (sending nothing); the broker hit its
  1024-fd ceiling, its accept loop EMFILE-stalled, and fresh clients (and the console) timed out for
  as long as the attacker held the sockets. Idle holds don't consume `MAX_PENDING_PER_USER` slots, so
  that cap doesn't bound it. Fully recovers on release.
- **Fix:** idle/read timeout on `serve()`; per-uid + global concurrent-connection caps; raise NOFILE.

### V4 — Uncapped `reason` written to the audit log → bloat / disk-fill  (MEDIUM, CONFIRMED)
- **Where:** the requester-controlled `reason` is written to `audit.jsonl` **in full**
  (`src/envh/core/audit.py` writes the whole record; only `_short`, the console display, truncates to
  80 chars). `control.py` `_reason()` applies no length cap.
- **Proof:** five `op_run`s with a 1,000,000-char reason were accepted and grew `audit.jsonl` by
  ~1,000,205 bytes each. Sustained, this fills the disk; on ENOSPC every broker audit write — including
  `vault.save` — fails. (Graded MEDIUM rather than HIGH: the uncapped-input + display/disk discrepancy
  is the novel core; the volumetric fill partly overlaps the acknowledged "no rate limits.")
- **Fix:** cap the stored `reason` length (and other free-text fields).

### V5 — Uncaught `ValueError`/`OverflowError` in duration parsing  (LOW, CONFIRMED)
- **Where:** `src/envh/core/durations.py` `parse_duration` does `int(digits)`; a >4300-digit value
  raises `ValueError` and a ~500-digit one `OverflowError` (via `timedelta`), neither wrapped as
  `DurationError`. `op_preset_validate` only catches `ConfigError`, so the client gets a misleading
  generic "internal error" and the broker logs a full traceback per request (log-flood primitive).
- **Fix:** wrap `int()`/`timedelta` and raise `DurationError`; bound the digit count in the regex.

### V6 — `op_import` pre-approval equality oracle on secret values  (MEDIUM, CONFIRMED — strongest)
- **What:** a non-root listed user can **confirm whether a guessed value equals a stored secret's
  value** (any named secret, including `CAPTURE_THE_FLAG`), **before any approval**, over the socket.
- **Where / mechanism:** `broker.request_import` (`src/envh/core/broker.py`) runs `import_resolution`
  — which classifies each submitted value by **equality** against the stored value (a correct value is
  "unchanged", so its deterministic rename target `NAME_2` is never created; a wrong value is renamed
  to `NAME_2` and added to the known-name set) — and then validates the renamed presets via
  `parse_presets(…, vault.names() | resolution['secrets'])` **before approval**. Submit (a) the guess
  under the secret's own name and (b) a preset mapping a var to `NAME_2`; the client-visible reply is
  **ERROR "secret NAME_2 is not in the vault" iff guess == stored value, else PENDING**.
- **Proof:** on a throwaway vault with a known value (driving the real broker code as `redteam`): only
  the byte-exact value → ERROR; same-length, long-shared-prefix, off-by-one, and short guesses all →
  PENDING — i.e. **exact full-value match, not a length/prefix leak**. Live on the real socket the two
  legs return different replies pre-approval, and `list` is byte-identical before/after (no state
  change). A correct guess raises before `new_request`, so it **allocates no request id and writes no
  "request" audit event — a hit is quieter than a miss.**
- **Impact:** 1 bit/query known-plaintext/dictionary oracle. It cannot brute the random `CTF-FLAG`
  sentinel, but it confirms low-entropy secrets (passwords, DB URLs) or any value an attacker already
  suspects, silently. `SECURITY.md` scopes "any way for a listed user to read a secret without the
  console approving it" as in-scope; this pre-approval socket-side oracle is unacknowledged.
- **Fix:** don't let a pre-approval reply depend on value equality — compute `import_resolution` only
  at approval time, or make the pre-approval validation independent of the rename-target names.

### V7 — Global `_next_id` leaks cross-user activity count  (LOW, CONFIRMED)
- **Where:** `src/envh/core/state.py` `StateTable._next_id` is one monotonic counter shared by
  `new_request` and `start_run`; the official client prints `request #N` on stderr.
- **Proof:** `redteam` noting its own ids before/after `redteam2` performs K operations recovers K
  exactly (gaps of 0/3/5 for K=0/3/5). `list`/`status` are uid-filtered, so this counter is the one
  channel that discloses another listed user's aggregate activity across the per-user boundary. No
  values/names/reasons leak.
- **Fix:** per-user id sequences, or don't return a global id to clients.

### V8 — Hook bypass class: 10 ways past the in-app passphrase warning  (MEDIUM×8 / LOW×2, CONFIRMED)
`claude/hooks/envh_ask.py` is meant to warn the human in-app whenever a command will start a
passphrase-requiring `envh` request. The README acknowledges 4 bypass *classes* (script file,
interpreter one-liner, pipe-into-shell, variable-built). These 10 are **different and novel**, several
defeating wrappers the hook *advertises* as handled. **Caveat:** the README explicitly says the hook
is "attention + a second chance," not the boundary — the console always re-prompts — so these matter
only when a human leans on the in-app ask. All reproduced on both the Claude Code (`tool_name:Bash`)
and Cursor (`beforeShellExecution`) input shapes.

- **uv/poetry value-options** (highest impact): `uv run --with <pkg> envh run …` (also `--python/-p`,
  `--project`, `--directory`, `--index-url`) → no-ask. RUNNERS have no value-option table, so the
  option's value is taken as the command name. A benign agent could legitimately emit this.
- **empty `ENVH_SESSION=`** env assignment → hook thinks "in session"; the client treats "" as no
  session and prompts.
- **empty `--session=` / `--session ''`** flag → same mismatch, different code path.
- **non-exported `ENVH_SESSION=abc; envh run …`** → counted as in-session but bash never exports it.
- **`ENVH_SESSION=x env -i envh …` / `env -u ENVH_SESSION envh …`** → counted, then stripped (LOW).
- **`env --split-string=` / `env -S`** → runs a nested command the hook never parses.
- **value-taking shell option before `-c`**: `bash -O extglob -c '…'`, `--rcfile`, `--init-file`,
  `sh -o OPT -c`.
- **`eval -- 'envh …'`** → leading `--` kept as the command name, so `basename != envh`.
- **eval/`bash -c` nesting past `MAX_NESTING=3`.**
- **deep `$()` nesting** (~500) hits the RecursionError fallback, where the `PASSPHRASE_FORM` regex
  misses `envh --socket=… run` (LOW, contrived).
- **Fix:** give RUNNERS a value-option table; parse `env -S`/`--split-string` and value-options-before-
  `-c` as nested scripts; match the client/broker truthiness for an empty session; drop in-session on
  `env -i`/`-u`; raise/replace the nesting cap with the regex fallback.

## Known-but-real (acknowledged in README; confirmed, not counted as new)
- **L1 — detached `session_start` requests linger ~10 min** (counting toward the per-uid 20-slot cap)
  after the client disconnects, unlike run/preset/import which free the slot immediately. A same-uid
  process can lock *itself* out of new sessions for ~10 min. Per-uid only; overlaps "rate limits
  (Not provided)". LOW.
- pid / cmdline / reason shown in prompts are unverified requester claims — acknowledged.

## Rejected (reproduced but no in-scope win — not useless-finding padding)
- **R1** — pid-reuse TOCTOU in the client's `terminate_lingering` raises an uncaught `PermissionError`
  that crashes **the attacker's own client**; the other user's process gets `EPERM` and lives. Self-
  harm, broker uninvolved. No win.
- **R2** — `envh import` crashes on a non-UTF-8 `.env` (`UnicodeDecodeError`); `scan`/`import` have no
  memory cap (~19–59× amplification on a crafted <25 MB file). Client-side only; no broker/exfil/
  approval impact. Hardening notes, not wins.

## What held (negative results worth keeping)
- Approval path: `approve`/`deny`/`create_session`/`env_for` are reachable **only** from the console
  (passphrase-gated); from the socket a request's decision can only ever become `withdrawn`. With no
  approver, no session is ever created, so the `op_run --session` auto-approve path has nothing to
  resolve. Verified by source-grep and live.
- No pre-decision write: `validate`/`propose`/`import` never touch `presets.yaml`/`config.yaml`/the
  vault before `approve()`; `list` byte-identical across blocking propose/import.
- Identity: bound to the kernel `SO_PEERCRED` uid read once at connect, immune to `SCM_RIGHTS`
  fd-passing; unlisted `outsider` rejected before any op. Session ids are 64-bit CSPRNG, never leaked
  to non-owners. Cross-uid use of session_wait/end/run blocked.
- No value/fingerprint leaked by `list`/`status`/`preset_validate`; propose/import carry the
  diff/fingerprints/resolution only in `request.summary` (console) and `request.result` (post-approval).
- Preset-backup path (importer) is traversal/symlink-safe; scanner regexes are linear (no ReDoS).

## Remediation priority
1. **V1/V2/V3** (HIGH broker DoS) — bound parse cost + offload YAML; add idle timeout + connection
   caps + `RLIMIT_AS`/`RLIMIT_NOFILE`. A single malicious agent/dependency with listed-user access can
   otherwise take the broker (and everyone's approvals) down with a few hundred bytes.
2. **V6** (import equality oracle) — move value-dependent resolution to approval time.
3. **V4/V5/V7** — cap stored free-text; wrap duration parsing; per-user ids.
4. **V8** — tighten the hook parser (defense-in-depth; not the boundary).

## Method & coordination notes
- 12 attack agents (Wave 1 = 4 smoke, Wave 2 = 8) across 12 disjoint surface families, each writing a
  structured findings file; 4 independent adversarial graders reproduced every candidate as non-root
  and graded it against the frozen rubric. Full artifacts in `/opt/ctf/arena/` (findings/ + grading/).
- Coordination lessons applied between waves: parallel agents can't read each other's ledger (so the
  event-loop-wedge was independently found twice — complementary root causes), fixed by feeding Wave 2
  a digest of Wave 1; and destructive DoS on a shared broker degraded concurrent agents, fixed by
  giving the DoS agent its own isolated broker. Anti-cheat held: every "win" was reproduced by a
  non-root grader and every root-flavored observation was dropped.
