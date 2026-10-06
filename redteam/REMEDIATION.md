# Remediation — fixes for the confirmed red-team findings

Every CONFIRMED finding in `FINDINGS.md` is fixed here, with a regression test. The REJECTED and
KNOWN items are not code-changed (they are out of scope or acknowledged). Each fix was also
re-validated live against the running broker as the non-root `redteam` user (the exploit no longer
works). Full suite: 559 passed (the only failures are 3 pre-existing `test_install.py` cases that
require a non-root runner — they fail identically on the unmodified baseline because the suite here
runs as root with no `SUDO_USER`).

| Finding | Fix | File(s) | Test |
|---|---|---|---|
| **V6** import equality oracle | The pre-approval validation now checks the SUBMITTED presets against the names the client provided (value-independent), not the post-dedup rename targets, so the reply no longer depends on whether a value matches a stored secret. The authoritative check on the renamed presets still runs at approval time. | `core/broker.py` `request_import` | `test_broker.py::test_import_does_not_leak_whether_a_submitted_value_matches_a_stored_secret` |
| **V1** alias-bomb `repr()` → wedge/OOM | Error messages describe a parsed value with a safe, bounded `_describe()` that never `repr()`s a container (YAML alias graphs) or `str()`s an oversized int. | `core/config.py` `_describe` + all value interpolations | `test_config.py::test_a_yaml_alias_bomb_in_a_preset_is_a_bounded_error_not_a_huge_string` |
| **V2** synchronous large-YAML wedge | `validate_preset_yaml` refuses a document over 64 KiB before parsing it on the event loop (presets are tiny). | `core/broker.py` `MAX_PRESET_BYTES` | `test_broker.py::test_oversized_preset_document_is_refused_before_parsing` |
| **V3** fd-exhaustion | Per-uid (32) and global (256) concurrent-connection caps; a 30 s idle timeout on the first request line; the broker raises its own `RLIMIT_NOFILE` to the hard limit. | `server/control.py` (`MAX_CONNECTIONS*`, `FIRST_LINE_TIMEOUT_SECONDS`), `server/hardening.py` | `test_control.py::test_one_user_cannot_hold_more_than_the_connection_cap`, `::test_an_idle_connection_is_dropped` |
| **V4** audit disk-fill via uncapped `reason` | `reason` is capped at 1000 characters at the socket boundary. | `server/control.py` `_reason` / `MAX_REASON` | `test_control.py::test_an_overlong_reason_is_refused_before_any_request_exists` |
| **V5** uncaught duration `ValueError`/`OverflowError` | The duration regex bounds the digit count (1–8), and `timedelta` construction catches `OverflowError` → `DurationError`. | `core/durations.py` | `test_durations.py::test_absurdly_long_durations_are_clean_errors_not_crashes`, `test_config.py::test_an_absurd_duration_in_a_preset_is_a_config_error` |
| **V7** cross-user `_next_id` count leak | Request/run ids are random and unique, not a global monotonic counter, so a client's own id reveals nothing about other users' activity. | `core/state.py` `_allocate_id` | `test_state.py::test_request_and_run_ids_are_unique_and_not_a_global_sequence` |
| **V8** hook bypasses (10) | Runner value-option table (uv/poetry); `env -S`/`--split-string` parsed as a nested script; value-taking shell options before `-c` skipped; `eval` drops a leading `--`; empty `ENVH_SESSION=`/`--session=` is not a session; a bare unexported `ENVH_SESSION=` no longer counts; `env -i`/`-u ENVH_SESSION` clears a counted session; nesting past the cap falls back to the word-match; the fallback regex allows a `--socket` option before the subcommand. | `claude/hooks/envh_ask.py` | `test_hook.py` (regression block; both Claude Code and Cursor shapes) |

Note on V8: the Claude/Cursor hook is explicitly **not** a security boundary (the console always
re-prompts for the vault passphrase). These fixes harden the in-app "a passphrase will be needed"
heads-up so it is not silently skipped; they do not change the trust model.
