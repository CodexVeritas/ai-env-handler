# Remediation — fixes for the confirmed red-team v2 findings

Every CONFIRMED finding (V9–V16) is fixed here with a regression test that fails on the pre-fix code
and passes after. The REJECTED detection gap is not code-changed (see `COVERAGE.md`). The full suite
passes; the only failures are the 3 pre-existing `test_install.py` cases that require a non-root
runner (they fail identically on the unmodified baseline when the suite runs as root with no
`SUDO_USER`, and pass in CI).

| Finding | Fix | File(s) | Regression test |
|---|---|---|---|
| **V9** audit disk-fill | `run_done`'s `exit_code`/`lingering_terminated` are coerced to a bounded int (`_run_int`, clamped to ±2³¹) before `audit.event("run_end", …)`, and a run's `--with` items are capped (`MAX_WITH_ITEMS=256`) so the logged `vars` list and the delivered env can't be blown up. Mirrors the existing `MAX_REASON`/`logged_command` caps. | `server/control.py` (`_run_int`, `MAX_WITH_ITEMS`, `_with_items`, `_start_run`) | `test_redteam_v2_regressions.py::test_run_done_fields_are_clamped_so_a_client_cannot_flood_the_audit_log`, `::test_a_run_with_too_many_with_items_is_refused` |
| **V10** session-id exhaustion wedge | Live sessions are capped per-uid (`MAX_LIVE_SESSIONS_PER_USER=64`) and globally (`MAX_LIVE_SESSIONS=256`) in `create_session`; `_new_session_id` retries a bounded number of times (`SESSION_ID_RETRIES`) then falls back to a wide random id, so generation can never spin the event loop. | `core/state.py` (`create_session`, `_new_session_id`, caps) | `test_redteam_v2_regressions.py::test_live_sessions_are_capped_per_user_to_prevent_the_id_exhaustion_wedge`, `::test_new_session_id_terminates_when_the_word_space_is_full` |
| **V11** alias-amplified preset validation | `parse_presets` refuses a document with more than `MAX_TOTAL_ENV_ENTRIES=10000` variable mappings summed across presets (and `MAX_PRESETS=1000`), counted cheaply with `len()` before the per-preset regex validation, so an aliased shared node can't amplify that work. | `core/config.py` (`parse_presets`, `MAX_TOTAL_ENV_ENTRIES`, `MAX_PRESETS`) | `test_redteam_v2_regressions.py::test_an_aliased_env_reused_across_many_presets_is_refused_quickly` |
| **V12** YAML merge-key bomb | `_load_yaml` parses with a hardened loader (`_HardenedLoader`, a `SafeLoader` whose `flatten_mapping` rejects YAML merge keys `<<`) so the exponential expansion is refused before it runs; `RecursionError` is also caught → `ConfigError`. | `core/config.py` (`_HardenedLoader`, `_load_yaml`) | `test_redteam_v2_regressions.py::test_a_yaml_merge_key_is_refused_before_it_can_expand` |
| **V13** hook ANSI-C quoting | `ansi_c_quoted` now decodes bash `$'…'` escapes (`\xHH`, octal, `\u`/`\U`, `\cX`, and the named controls) via `ansi_c_escape`, so a reconstructed command name resolves to `envh`. | `claude/hooks/envh_ask.py` (`ansi_c_escape`, `ansi_c_quoted`) | `test_hook.py::test_ansi_c_quoted_command_name_is_decoded_so_envh_is_not_hidden` |
| **V14** hook Cursor matcher | The Cursor snippet drops the command-text `matcher`, and `setup_wizard.with_cursor_hook` no longer writes one, so the hook runs on every `beforeShellExecution` (as the Claude path runs on every Bash command) and sees reconstructed names. | `cursor/hooks.snippet.json`, `scripts/setup_wizard.py` | `test_hook.py::test_cursor_hook_has_no_command_text_matcher_and_acts_on_a_reconstructed_name`, `test_setup_wizard.py::test_cursor_hook_entry_matches_the_documented_snippet` |
| **V15** hook prefix wrappers | The wrapper/runner tables gain the confirmed exec-wrappers (`ionice`, `taskset`, `nocache`) and runners (`pipenv`, `pdm`, `rye`). The list is curated and **cannot** be exhaustive — wrappers with a positional argument before the command (`chrt PRIO`, `setarch ARCH`) are still not peeled; the hook is attention-only and a disguised command still prompts on the console, so this is a mitigation, not a boundary. | `claude/hooks/envh_ask.py` (`WRAPPERS`, `RUNNERS`) | `test_hook.py::test_added_exec_wrappers_and_runners_do_not_hide_envh` |
| **V16** robustness | At the op boundary: `minutes` is coerced/clamped (`_minutes`), `--with` items are string-coerced on the session-run path too (`_with_items`), `json.loads` failures are caught as `ValueError` (covers the huge-int-literal case) in `serve`/`op_search`/the run loop, and deeply nested YAML `RecursionError` → `ConfigError`. | `server/control.py`, `core/config.py` | `test_redteam_v2_regressions.py::test_a_non_finite_minutes_is_a_clean_error_not_an_overflow`, `::test_a_huge_json_number_is_a_clean_error_not_an_internal_error`, `::test_a_session_run_with_non_string_with_items_is_a_clean_error`, `::test_deeply_nested_yaml_is_a_clean_config_error_not_a_recursion_crash` |

## Notes

- **V9/V11/V12 are genuine bypasses** of the 2026-10-05 fixes (V4 capped only `reason`/`command`; V2
  capped only raw preset bytes; V1 hardened only the error path). The fixes here bound the specific
  surfaces those fixes missed, keeping the originals intact (their regression tests still pass).
- **V15 is a mitigation, not a closure.** An exec-wrapper allow-list is inherently incomplete and the
  hook is explicitly not a security boundary; a fully general fail-closed would ask on every
  unrecognized leading token at the cost of false positives on mentions (`echo envh run …`). The
  curated list closes the confirmed common wrappers; the console remains the boundary.
- **Residual (not a V-finding):** a rapid create+end session loop still accumulates ended-session
  objects until the 1 h prune (bounded memory, not the fixed wedge). Noted in `COVERAGE.md`.
