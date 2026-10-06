# Grading queue — consolidated distinct candidates (merge of Wave 1 + Wave 2)

Graders: reproduce INDEPENDENTLY as non-root (`redteam`/`redteam2`/`outsider`), then grade each per
CHARTER §6 (CONFIRMED / KNOWN / REJECTED) with severity, a novelty check against README.md +
SECURITY.md (§5), and the clause numbers you rely on. Be ADVERSARIAL: default to REJECTED unless
YOU reproduced it and it meets a §4 win condition and is novel. Paste your own reproduction output.
Finder files are in `/opt/ctf/arena/findings/`; some left repro scripts in `/tmp` (you may use them
but re-derive, don't just trust them).

## DoS / availability family (reproduce on the ISOLATED broker /run/envh-dos/ctl.sock ONLY)
- **D1 alias-bomb repr amplification → event-loop wedge AND full OOM crash** — w1-preset-1 + w2-dos-1.
  ~350–490 byte preset YAML with aliases → `op_preset_validate` builds an astronomically large
  `repr()` in a validation error; wedges the single asyncio loop and (w2-dos-1) OOM-killed the broker
  (dmesg-confirmed). No approval. Claimed HIGH.
- **D2 synchronous YAML parse blocks the event loop** — w1-protocol-2 (+ w2-dos-3 import/propose
  variant). `op_preset_validate` runs `yaml.safe_load` + validation synchronously on the loop; ~800KB
  input froze the broker ~6–8s; no approval. Claimed HIGH.
- **D3 fd-exhaustion** — w1-protocol-1. ~1020 idle connections from one listed user; no idle timeout,
  no connection cap; broker hits RLIMIT_NOFILE=1024 and stops serving all clients. Claimed HIGH.
- **D4 audit-log disk-fill via uncapped `reason`** — w2-dos-2. The `reason` string is written to
  `audit.jsonl` in FULL (console only truncates the *display* to 80 chars); a listed user writes
  ~1MB/op_run with no cap → fills the disk; on ENOSPC every broker audit write (incl. vault.save)
  fails. Claimed HIGH. (Confirm the non-root-triggerable uncapped write + code path; the disk-fill
  extrapolation is arithmetic — do NOT actually fill the real disk.)
- **D5 duration parse: uncaught ValueError/OverflowError** — w1-preset-2 + w2-dos-4. A >4300-digit or
  ~500-digit `max_session` escapes as a generic "internal error" + a broker traceback per request
  (log-flood). Claimed LOW.

## Info-leak
- **I1 `op_import` pre-approval equality oracle on secret VALUES** — w2-infoleak-1. `request_import`
  runs `import_resolution` (classifies a submitted value by equality vs the stored one) then validates
  renamed presets BEFORE approval; by pointing a preset at the `NAME_2` rename target, the client-
  visible reply flips ERROR iff guess == stored value, else PENDING. A 1-bit/query known-plaintext
  oracle on any named secret (incl. CAPTURE_THE_FLAG), non-root, no approval. Claimed MEDIUM.
  SECURITY.md scopes "unapproved secret reads" as IN scope. **Grade rigorously** — this is the
  strongest candidate. Prove exact-match (not prefix/length) via the real broker code against a
  throwaway vault with a KNOWN value you set, AND confirm pre-approval reachability live over the main
  socket as redteam (reply shape differs; you need not know the real flag value).
- **I2 global `_next_id` cross-user activity-count leak** — w2-crossuser. `StateTable._next_id` is one
  global counter for requests AND runs; the client prints `request #N` on stderr, so a listed user can
  infer the COUNT of another listed user's allocations between two of its own. Aggregate metadata only
  (no values). Claimed LOW.

## Hook (attention bypass — note: README states the hook is NOT a security boundary)
- **H ~10 parser gaps** — w1-hook-1..5 + w2-hook-1..5. Commands that DO start an envh passphrase
  request but the hook returns NO-ASK (or wrong reason), beyond the README's acknowledged classes
  (script file / interpreter one-liner / pipe-into-shell / variable-built). Reproduce ALL; for each
  confirm (a) hook output is no-ask, (b) the command really starts a request. Grade novelty + severity
  as a class (bounded because the console remains the real boundary). Test both the Claude Code
  (`tool_name:Bash`) and Cursor (`beforeShellExecution`) input shapes.

## Lifecycle / misc + validate the self-rejections
- **L1 detached `session_start` linger** — w1-session-1. Detached session requests stay pending the
  full 10-min grace (unlike run/preset/import which withdraw on disconnect), so a same-uid process can
  fill its own 20-slot quota and lock itself out ~10 min. Per-uid only. Claimed LOW/KNOWN.
- **R1 (validate REJECTION)** w2-clientrun-1 — pid-reuse TOCTOU raises PermissionError that crashes the
  ATTACKER'S OWN client; no cross-user signal. Confirm it is correctly REJECTED (self-harm, no win).
- **R2 (validate REJECTION)** w2-supply LOWs — `envh import` crashes on non-UTF-8 `.env`
  (UnicodeDecodeError); `scan`/`import` have no memory cap (~57x amplification on a crafted <25MB
  file). Client-side only, no broker/exfil. Confirm correctly REJECTED (or argue LOW-keep).
