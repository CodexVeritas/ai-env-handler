#!/usr/bin/env bash
# Tier 2 gate tests: the console-hardening assurances that only a VM can validate.
#
# For each gate we flip the host-kernel state and assert envh's OWN startup check behaves as the
# README promises -- the positive (hardened) state passes, and every DEGRADED state FAILS CLOSED
# with a clear error (never silently). This is the negative/assurance testing a container cannot do
# because dev.tty.legacy_tiocsti and kernel.yama.ptrace_scope are non-namespaced.
#
# Usage: sudo bash gate_tests.sh [VENV]    (default /opt/arena-venv). Must be root in a VM.
set -uo pipefail
VENV="${1:-/opt/arena-venv}"
PY="$VENV/bin/python"
fail=0
ok()  { echo "  PASS  $*"; }
bad() { echo "  FAIL  $*"; fail=1; }

# assert_no_tiocsti() raises HardeningError unless legacy_tiocsti is present AND 0.
check_tiocsti() {  # $1 = expected: "pass" | "raise"
  "$PY" - "$1" <<'PY'
import sys
from envh.server.hardening import assert_no_tiocsti, HardeningError
expected = sys.argv[1]
try:
    assert_no_tiocsti()
    got = "pass"
except HardeningError as e:
    got = "raise"; sys.stderr.write(f"(raised: {e})\n")
sys.exit(0 if got == expected else 1)
PY
}

# envh's installer/startup preflight flags a Yama problem when ptrace_scope is 0 or the LSM is absent.
check_yama_preflight() {  # $1 = expected: "problem" | "clear"
  "$PY" - "$1" <<'PY'
import sys
from envh.platform import preflight_problems
expected = sys.argv[1]
has = any(("Yama LSM is not available" in p) or ("kernel.yama.ptrace_scope is 0" in p) for p in preflight_problems(None))
sys.exit(0 if ("problem" if has else "clear") == expected else 1)
PY
}

echo "== GATE: dev.tty.legacy_tiocsti (console keystroke-injection defense) =="
if [[ -e /proc/sys/dev/tty/legacy_tiocsti ]]; then
  sysctl -w dev.tty.legacy_tiocsti=0 >/dev/null
  check_tiocsti pass  && ok "legacy_tiocsti=0 -> envh accepts the host (hardened)"      || bad "legacy_tiocsti=0 should pass"
  sysctl -w dev.tty.legacy_tiocsti=1 >/dev/null
  check_tiocsti raise && ok "legacy_tiocsti=1 -> envh refuses, fails closed (degraded)" || bad "legacy_tiocsti=1 should fail closed"
  sysctl -w dev.tty.legacy_tiocsti=0 >/dev/null   # restore
else
  check_tiocsti raise && ok "no legacy_tiocsti control -> envh refuses (kernel < 6.2)"  || bad "missing control should fail closed"
fi

echo "== GATE: kernel.yama.ptrace_scope (anti-ptrace of the broker) =="
if [[ -e /proc/sys/kernel/yama/ptrace_scope ]]; then
  for v in 0 1 2; do
    sysctl -w kernel.yama.ptrace_scope=$v >/dev/null
    expected=clear; [[ "$v" == 0 ]] && expected=problem
    if check_yama_preflight "$expected"; then ok "ptrace_scope=$v -> envh preflight reports the expected state ($expected)"; else bad "ptrace_scope=$v -> envh preflight reported the wrong state (wanted $expected)"; fi
  done
  sysctl -w kernel.yama.ptrace_scope=1 >/dev/null   # restore
else
  if check_yama_preflight problem; then ok "Yama node absent -> envh preflight flags it (fails closed)"; else bad "Yama node absent -> envh preflight did not flag it"; fi
fi

echo "== GATE: /dev/uinput (virtual-keyboard injection surface) =="
if [[ -e /dev/uinput ]]; then
  perms=$(stat -c '%a' /dev/uinput)
  echo "  /dev/uinput present, mode $perms"
  if [[ "$perms" =~ [2367]$ ]]; then bad "/dev/uinput is world-writable (mode $perms) -> degraded mode; the README says approve elsewhere"; else ok "/dev/uinput not world-writable (mode $perms)"; fi
else
  ok "/dev/uinput absent (no virtual-keyboard injection surface on this guest)"
fi

echo "== Display server (keylog/inject surface) =="
echo "  XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-none} (X11 is a documented degraded mode; Wayland is safer). Headless VM: typically none."

echo
if [[ "$fail" == 0 ]]; then echo "GATE TESTS: PASS -- hardened states accepted, degraded states fail closed."; else echo "GATE TESTS: FAIL -- a gate did not behave as documented."; fi
exit "$fail"
