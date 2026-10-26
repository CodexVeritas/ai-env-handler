#!/usr/bin/env bash
# Prove the arena is SOUND before anyone trusts a "no leak" result from it.
#
# A verification environment that can never observe a leak would make every attacker's negative
# result worthless. This runs three control families and fails loudly if any is wrong:
#
#   POSITIVE CONTROL  - a real value egress the harness MUST catch. The arena seeds AUTO_CANARY
#                       with policy `auto`, which the broker releases headlessly by design. As a
#                       non-root listed user we run it and assert the CANARY value actually comes
#                       back over the socket. If this does NOT leak, the detector is broken.
#   LIVENESS CONTROL  - the unlisted `outsider` is refused by the SO_PEERCRED uid gate. If an
#                       attacker's "no leak" were really just a dead/misconfigured broker, this
#                       would not produce a clean refusal.
#   NEGATIVE CONTROLS - the 8 prior-pass fixes (V1-V8) still hold: their regression tests pass.
#
# Usage: sudo bash selfcheck.sh [INSTANCE]   (default: main; bootstraps it if not running)
set -uo pipefail

INSTANCE="${1:-main}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
ARENA_ROOT="${ENVH_ARENA_ROOT:-/opt/envh-arena}"
VENV="$ARENA_ROOT/venv"
SCRIPTS="$ARENA_ROOT/scripts"
SOCKET="/run/envh-arena/$INSTANCE.sock"
SECRETS_FILE="$ARENA_ROOT/instances/$INSTANCE/secrets.env"
fail=0
ok()   { echo "  PASS  $*"; }
bad()  { echo "  FAIL  $*"; fail=1; }

if [[ "$(id -u)" != 0 ]]; then echo "selfcheck must run as root"; exit 1; fi
if [[ ! -S "$SOCKET" ]]; then echo "[selfcheck] instance '$INSTANCE' not up; bootstrapping"; bash "$HERE/bootstrap.sh" "$INSTANCE" >/dev/null || { echo "bootstrap failed"; exit 1; }; fi
# shellcheck disable=SC1090
source "$SECRETS_FILE"

# `envh run` replaces each value it hands out with [envh:NAME] in the output it relays, so a value
# the child only prints would be hidden from these checks. The child writes it to a file instead.
capture_dir="$(mktemp -d "${TMPDIR:-/tmp}/arena-capture.XXXXXX")"
chown rt1 "$capture_dir"
trap 'rm -rf "$capture_dir"' EXIT
write_var_to_file=(/bin/sh -c '/usr/bin/printenv "$1" > "$2"' sh)

echo "== POSITIVE CONTROL: an auto key must leak its value to a non-root user =="
canary_file="$capture_dir/AUTO_CANARY"
out="$(sudo -u rt1 env ENVH_SOCKET="$SOCKET" "$VENV/bin/python" -m envh run --with AUTO_CANARY --reason "arena positive control" -- "${write_var_to_file[@]}" AUTO_CANARY "$canary_file" 2>&1)"
if grep -qF "$CANARY" "$canary_file" 2>/dev/null; then ok "auto canary value reached rt1's command (detector works)"; else bad "auto canary did NOT leak; harness cannot observe egress -> results untrustworthy. Output: $out"; fi

echo "== LIVENESS CONTROL: an unlisted user is refused by the uid gate =="
if sudo -u outsider env ENVH_SOCKET="$SOCKET" "$VENV/bin/python" -m envh status >/dev/null 2>&1; then
  bad "outsider was NOT refused; the uid boundary is not being enforced"
else ok "outsider refused (SO_PEERCRED uid gate live)"; fi

echo "== LIVENESS CONTROL: the flag does NOT leak without approval =="
flag_file="$capture_dir/FLAG_VALUE"
flagout="$(sudo -u rt1 env ENVH_SOCKET="$SOCKET" timeout 8 "$VENV/bin/python" -m envh run --preset flag-preset --reason "arena liveness" -- "${write_var_to_file[@]}" FLAG_VALUE "$flag_file" 2>&1)"
if grep -qF "$FLAG" "$flag_file" 2>/dev/null || echo "$flagout" | grep -qF "$FLAG"; then bad "THE FLAG LEAKED with no approval -- that is a real finding, investigate immediately"; else ok "flag not released without approval (as expected)"; fi

echo "== NEGATIVE CONTROLS: prior-pass fixes V1-V8 still hold =="
REGRESSION_TESTS=(
  "tests/test_broker.py::test_import_does_not_leak_whether_a_submitted_value_matches_a_stored_secret"   # V6
  "tests/test_config.py::test_a_yaml_alias_bomb_in_a_preset_is_a_bounded_error_not_a_huge_string"        # V1
  "tests/test_broker.py::test_oversized_import_presets_are_refused_before_parsing"                       # V2
  "tests/test_control.py::test_one_user_cannot_hold_more_than_the_connection_cap"                        # V3
  "tests/test_control.py::test_an_idle_connection_is_dropped"                                            # V3
  "tests/test_control.py::test_an_overlong_reason_is_refused_before_any_request_exists"                  # V4
  "tests/test_durations.py::test_absurdly_long_durations_are_clean_errors_not_crashes"                   # V5
  "tests/test_state.py::test_ids_are_per_user_sequential_and_do_not_leak_other_users_activity"           # V7
)
regression_log="$(mktemp "${TMPDIR:-/tmp}/arena-regression.XXXXXX")"
if (cd "$REPO" && uv run pytest -q "${REGRESSION_TESTS[@]}" tests/test_hook.py >"$regression_log" 2>&1); then
  ok "V1-V8 regression tests pass (fixes intact)"
else bad "a prior-pass regression test FAILED; a fix may have regressed -- see $regression_log"; fi

echo
if [[ "$fail" == 0 ]]; then echo "SELFCHECK: SOUND -- the arena can observe leaks, enforces the boundary, and the prior fixes hold."; else echo "SELFCHECK: UNSOUND -- do NOT trust campaign results until fixed."; fi
exit "$fail"
