#!/usr/bin/env bash
# Tear down an arena instance (or everything).
#
# Usage:
#   sudo bash teardown.sh [INSTANCE]   # stop one instance's broker, remove its data (default: main)
#   sudo bash teardown.sh --all        # stop all instances, remove users and the whole arena root
set -euo pipefail

ARENA_ROOT="${ENVH_ARENA_ROOT:-/opt/envh-arena}"
SVC_USER="envh-arena-svc"
USERS=(rt1 rt2 outsider)
if [[ "$(id -u)" != 0 ]]; then echo "teardown must run as root"; exit 1; fi

stop_instance() {
  local inst="$1"
  local pidfile="$ARENA_ROOT/instances/$inst/broker.pid"
  if [[ -f "$pidfile" ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    sleep 0.3; kill -9 "$(cat "$pidfile")" 2>/dev/null || true
  fi
  rm -f "/run/envh-arena/$inst.sock"
  rm -rf "$ARENA_ROOT/instances/$inst"
  echo "[arena] stopped instance $inst"
}

if [[ "${1:-}" == "--all" ]]; then
  if [[ -d "$ARENA_ROOT/instances" ]]; then
    for d in "$ARENA_ROOT"/instances/*/; do [[ -d "$d" ]] && stop_instance "$(basename "$d")"; done
  fi
  pkill -u "$SVC_USER" 2>/dev/null || true
  for u in "${USERS[@]}" "$SVC_USER"; do id "$u" >/dev/null 2>&1 && userdel -r "$u" 2>/dev/null || true; done
  rm -rf "$ARENA_ROOT" /run/envh-arena
  echo "[arena] removed users and $ARENA_ROOT"
else
  stop_instance "${1:-main}"
fi
