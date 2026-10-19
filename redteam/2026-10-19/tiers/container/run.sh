#!/usr/bin/env bash
# Tier 1 in-container run: stand up the arena, prove it sound, run the uid/protocol-level checks.
# Exits nonzero if the arena is unsound or a boundary check fails. The hardening gates are NOT
# checked here (shared kernel) — see ../vm/ for those.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../../.."   # repo root

echo "== Tier 1 (container, distro=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")) =="
echo "kernel (shared with host): $(uname -r)"

bash redteam/2026-10-19/arena/bootstrap.sh main
bash redteam/2026-10-19/arena/selfcheck.sh main
echo "Tier 1 OK: uid boundary, socket protocol, and prior fixes validated on this userspace."
