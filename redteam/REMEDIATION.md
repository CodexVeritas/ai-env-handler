# Remediation — fixes for the confirmed red-team findings

Every CONFIRMED finding in `FINDINGS.md` is fixed here, with a regression test. The REJECTED and
KNOWN items are not code-changed (they are out of scope or acknowledged). Each fix was also
re-validated live against the running broker as the non-root `redteam` user (the exploit no longer
works). The full suite passes (the only failures are 3 pre-existing `test_install.py` cases that
require a non-root runner — they fail identically on the unmodified baseline because the suite here
runs as root with no `SUDO_USER`; CI runs them non-root and they pass).

| Finding | Fix | File(s) | Test |
|---|---|---|---|
| **V6** import equality oracle | The pre-approval validation now checks the SUBMITTED presets against the names the client provided (value-independent), not the post-dedup rename targets, so the reply no longer depends on whether a value matches a stored secret. The authoritative check on the renamed presets still runs at approval time. | `core/broker.py` `request_import` | `test_broker.py::test_import_does_not_leak_whether_a_submitted_value_matches_a_stored_secret` |
| **V1** alias-bomb `repr()` → wedge/OOM | Error messages describe a parsed value with a safe, bounded `_describe()` that never `repr()`s a container (YAML alias graphs) or `str()`s an oversized int. | `core/config.py` `_describe` + all value interpolations | `test_config.py::test_a_yaml_alias_bomb_in_a_preset_is_a_bounded_error_not_a_huge_string` |
| **V2** synchronous large-YAML wedge | Presets over 64 KiB are refused before parsing on the event loop, both in `validate_preset_yaml` and in `request_import` (presets are tiny). | `core/broker.py` `refuse_oversized_presets` | `test_broker.py::test_oversized_preset_document_is_refused_before_parsing`, `::test_oversized_import_presets_are_refused_before_parsing` |
| **V3** fd-exhaustion | Per-uid (32) and global (256) concurrent-connection caps; a 30 s idle timeout on the first request line; the broker raises its own `RLIMIT_NOFILE` to the hard limit (when that limit is finite). | `server/control.py` (`MAX_CONNECTIONS*`, `FIRST_LINE_TIMEOUT_SECONDS`), `server/hardening.py` | `test_control.py::test_one_user_cannot_hold_more_than_the_connection_cap`, `::test_an_idle_connection_is_dropped` |
| **V4** audit disk-fill via uncapped `reason` | `reason` is capped at 1000 characters at the socket boundary, and the audit log keeps the first 4000 characters of a command line (a run's, or an unlisted uid's). | `server/control.py` `_reason` / `MAX_REASON`, `logged_command` | `test_control.py::test_an_overlong_reason_is_refused_before_any_request_exists`, `::test_the_audit_log_keeps_only_the_start_of_a_long_command`, `test_multiuser.py::test_an_unlisted_uid_cannot_write_a_long_command_line_to_the_audit_log` |
| **V5** uncaught duration `ValueError`/`OverflowError` | The duration regex bounds the digit count (1–8), so neither error can occur. | `core/durations.py` | `test_durations.py::test_absurdly_long_durations_are_clean_errors_not_crashes`, `test_config.py::test_an_absurd_duration_in_a_preset_is_a_config_error` |
| **V7** cross-user `_next_id` count leak | Request/run ids are now a **per-user sequence** (each user counts its own `#1`, `#2`, … shared across its requests and runs), with the live tables keyed by `(uid, seq)`, instead of one global monotonic counter — so a client's own id reveals nothing about other users' activity, and the ids stay short and readable on the console. The console feed/logs label each id with the requester (`alice#3`) to tell two users' sequences apart. | `core/state.py` `_allocate_id`, `server/console/activity.py` | `test_state.py::test_ids_are_per_user_sequential_and_do_not_leak_other_users_activity` |
| **V8** hook bypasses (10) | Runner value-option table (uv/poetry); `env -S`/`--split-string` parsed as a nested script; value-taking shell options before `-c` skipped; `eval` drops a leading `--`; empty `ENVH_SESSION=`/`--session=` is not a session; a bare unexported `ENVH_SESSION=` no longer counts; `env -i`/`env -`/`-u ENVH_SESSION`/`-uENVH_SESSION` clears a counted session; nesting past the cap falls back to the word-match; the fallback regex allows a `--socket` option before the subcommand. | `claude/hooks/envh_ask.py` | `test_hook.py` (regression block; both Claude Code and Cursor shapes) |

Note on V8: the Claude/Cursor hook is explicitly **not** a security boundary (the console always
re-prompts for the vault passphrase). These fixes harden the in-app "a passphrase will be needed"
heads-up so it is not silently skipped; they do not change the trust model.

## Client-tool hardening (the two REJECTED robustness gaps)

Two supply-chain findings were graded REJECTED (client-side only, no broker impact, no §4 win) but
were real robustness gaps, now fixed in `tools/importer.py`:

- **A non-UTF-8 `.env` crashed `envh import`** with a traceback. It now stops with a clear error naming
  the file and line. (Decoding with replacement, as the scanner does, would store a wrong secret and rewrite
  untouched lines, since the importer writes files back.) Test:
  `test_importer.py::test_parse_env_file_refuses_non_utf8_bytes_naming_the_line`.
- **`envh import` had no size cap** — a large file was parsed whole, while the scanner already caps at
  25 MB. `parse_env_file` now refuses a file over `MAX_IMPORT_BYTES` (25 MB) with a clear error. Test:
  `test_importer.py::test_parse_env_file_refuses_an_oversized_file`.
