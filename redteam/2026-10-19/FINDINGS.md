# Findings — envh red-team v2 (2026-10-19)

Final report. Every finding below was proposed by an attacker agent, then **independently graded** by
a separate agent that reproduced it from scratch as a **non-root user** on its own fresh arena
instance (verdicts and pasted reproductions in `evidence/grading/`). Numbering continues from the
2026-10-05 pass (V1–V8); the new findings are **V9–V16**.

## Headline

The confidentiality/integrity core held again: across 9 diverse attacker agents plus an automated
large-N fuzz sweep, **no secret value was exfiltrated, no approval was forged or bypassed, no
policy/preset/vault was written without approval, and cross-user isolation and the `SO_PEERCRED`
identity model were not broken** (F6 identity, F7 client-run, F8 info-leak, F11 supply-chain, F12
cross-user all came back sound — see the per-agent files). What this pass surfaced is **availability
(DoS)**: four novel ways for a single listed user to wedge or flood the single-threaded broker, each
bypassing a 2026-10-05 fix or an uncapped surface; plus three **hook-attention** bypasses (the hook
is explicitly not the boundary) and a class of **low-severity robustness** gaps. All are fixed here
with regression tests (`REMEDIATION.md`).

## CONFIRMED (novel)

| ID | Sev | Surface | Title | Taxonomy |
|----|-----|---------|-------|----------|
| **V9**  | HIGH | F14/F1 | **Audit-log disk-fill via uncapped `run_done` fields + `vars` (V4 bypass).** An `auto`-key run (granted with no console) can send a `run_done` note whose `exit_code`/`lingering_terminated` are logged verbatim and uncapped to the never-trimmed `audit.jsonl`; the per-request `vars` list is likewise uncapped. Measured ~25 MB/s from one non-root user → disk-fill DoS of the broker + audit partition, and it drowns real events out of the console's bounded tail. V4 capped only `reason`/`command`. | ATT&CK T1499, ASVS V7 |
| **V10** | HIGH | F3 | **Session-id namespace exhaustion wedges the broker.** No cap on live sessions + a fixed 176×204 = 35,904 adjective-noun id space + an unbounded `while True` retry in `_new_session_id`: one listed user (via the auto preset, no approver) fills the space in ~140 s, after which id generation spins forever and freezes the single event loop permanently (a second user's `status` times out). | ATT&CK T1499, ASVS V11 |
| **V11** | HIGH | F9 | **Alias-amplified preset validation wedges the loop (bypasses V1/V2).** A *valid* preset YAML under the 64 KiB cap that aliases one shared `env` node into ~1,500 presets makes `parse_preset` re-validate the same node per preset — O(presets × vars) ≈ 3M regex checks, ~7 s of synchronous block per request, sustained via concurrent connections. V2 caps only raw bytes; V1 hardened only the error path. | ATT&CK T1499.004, LLM10, ASVS V11 |
| **V12** | HIGH | F9/F5 | **YAML merge-key (`<<`) expansion bomb (bypasses V1/V2).** A ~500-byte document expands width**depth inside `yaml.safe_load`'s `flatten_mapping` — before any validation — via the no-approval `op_preset_validate`/`op_preset_propose`; attacker-chosen block time, depth ~10 OOM-kills. Tiny payload evades the byte cap; blow-up precedes the V1 error path. | ATT&CK T1499.004, LLM10, CWE-776, ASVS V5 |
| **V13** | MEDIUM | F10 | **Hook: ANSI-C quoting not decoded.** `$'\x65nvh' …` runs as `envh`, but the hook's lexer stripped the backslash without decoding `\xHH`/octal/unicode, so the command name didn't match and no "a passphrase will be needed" heads-up fired. | LLM01, ATLAS AML.T0051 |
| **V14** | MEDIUM | F10 | **Hook: Cursor's matcher was a bare `"envh"` substring.** Commands where bash reconstructs the name with no literal `envh` (`e""nvh`, the ANSI-C form) never invoked the hook at all (silent allow). The Claude path (tool-name matcher) was unaffected. | LLM01 |
| **V15** | MEDIUM | F10 | **Hook: unlisted prefix wrappers hide envh.** `ionice`/`taskset`/`chrt`/`setarch` and `pipenv`/`pdm`/`rye run` weren't in the allow-list peeler, so the real `envh` command after them was missed. (Same shape as the V8 runner-table work; the wrapper list is inherently curated — see REMEDIATION.) | LLM01 |
| **V16** | LOW | F1/F2/F5 | **Robustness: client inputs cause an uncaught "internal error".** Four client-triggerable uncaught exceptions, each a per-connection failure (broker survives, no value/traceback leaks): `op_session_start` integer `minutes` overflow (a V5 gap — the fix covered duration *strings*, not the int field), a huge JSON number literal raising `ValueError` not `JSONDecodeError`, a session-run `--with` list with non-string items, and deeply nested YAML raising `RecursionError`. | ASVS V5 |

## KNOWN / REJECTED

- **Detection gap (read ops emit no audit)** — graded **REJECTED** as a vulnerability (optional
  defense-in-depth, not an access or integrity break): `list`/`status`/`needs_approval`/
  `preset_validate` succeed with no audit event, so enumeration is invisible. Not fixed (it would add
  log noise for no security gain); noted in `COVERAGE.md` as a possible future hardening.
- All the 2026-10-05 **V1–V8** issues were re-attacked for *bypasses*. V9/V11/V12 are genuine
  bypasses of V1/V2/V4 (counted as novel above); the fixes themselves still hold on current `main`
  (their regression tests pass — the arena self-check's negative controls).

## Severity note

V9–V12 are all availability (a listed user denying the broker/console); none yields a secret, an
approval, or cross-user access. Consistent with the product's own threat model, the kernel-enforced
confidentiality boundary continued to hold; the gaps are in bounding the work and the log-writes a
single unapproved (auto-key) client can trigger.
