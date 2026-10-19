#!/usr/bin/env bash
# Stand up a faithful, headless envh red-team arena (Tier 0).
#
# Creates a dedicated broker SERVICE user plus unprivileged "listed" users (rt1, rt2) and an
# unlisted outsider, builds a self-contained venv those users can execute, seeds a fresh vault
# with a random CAPTURE_THE_FLAG (never written to disk in plaintext), and launches the SAME
# production broker + control-socket code path headlessly with NO approver. Attackers then hit
# the socket only as rt1/rt2/outsider via `sudo -u` -- so the kernel uid boundary (SO_PEERCRED),
# 0600 vault ownership, and cross-user isolation are all exercised for real.
#
# Idempotent for users/venv/scripts; each named instance gets its own data dir, socket, and flag
# (use a fresh instance for destructive/DoS tests so you don't wedge the shared one).
#
# Usage:   sudo bash bootstrap.sh [INSTANCE]      (default instance: main)
# Must run as root. The flag/passphrase are captured here and kept 0600 root, not readable by
# the attacker users.
set -euo pipefail

INSTANCE="${1:-main}"
ARENA_ROOT="${ENVH_ARENA_ROOT:-/opt/envh-arena}"
REPO="${ENVH_ARENA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SVC_USER="envh-arena-svc"
LISTED_USERS=(rt1 rt2)
UNLISTED_USER="outsider"
PY="/usr/bin/python3.12"
VENV="$ARENA_ROOT/venv"
SCRIPTS="$ARENA_ROOT/scripts"
BIN="$ARENA_ROOT/bin"
RUN_DIR="/run/envh-arena"
DATA="$ARENA_ROOT/instances/$INSTANCE/data"
SOCKET="$RUN_DIR/$INSTANCE.sock"
SECRETS_FILE="$ARENA_ROOT/instances/$INSTANCE/secrets.env"   # 0600 root: PASS/FLAG/CANARY
PIDFILE="$ARENA_ROOT/instances/$INSTANCE/broker.pid"
LOG="$ARENA_ROOT/instances/$INSTANCE/broker.log"

if [[ "$(id -u)" != 0 ]]; then echo "bootstrap must run as root (it creates users)"; exit 1; fi
command -v uv >/dev/null || { echo "uv not found on PATH"; exit 1; }

say() { echo "[arena] $*"; }

# --- users (idempotent) ---------------------------------------------------------------------
ensure_user() {
  local user="$1"; shift
  if ! id "$user" >/dev/null 2>&1; then
    useradd "$@" "$user"
    say "created user $user"
  fi
}
ensure_user "$SVC_USER" --system --create-home --home-dir "$ARENA_ROOT/svc-home" --shell /usr/sbin/nologin
for u in "${LISTED_USERS[@]}" "$UNLISTED_USER"; do ensure_user "$u" --create-home --shell /bin/bash; done

# --- arena venv (built once, world-readable so unprivileged users can execute it) -----------
if [[ ! -x "$VENV/bin/python" ]]; then
  say "building arena venv at $VENV (python $PY)"
  mkdir -p "$ARENA_ROOT"
  uv venv "$VENV" --python "$PY" >/dev/null
  uv pip install --quiet --python "$VENV/bin/python" "$REPO" >/dev/null
  say "installed envh into arena venv"
fi

# --- copy the arena scripts next to the venv, world-readable --------------------------------
mkdir -p "$SCRIPTS" "$BIN" "$RUN_DIR"
cp "$(dirname "${BASH_SOURCE[0]}")"/*.py "$SCRIPTS/"
chmod -R a+rX "$ARENA_ROOT" 2>/dev/null || true
chown "$SVC_USER":"$SVC_USER" "$RUN_DIR"; chmod 755 "$RUN_DIR"

# --- wrapper CLIs: `envh-rt1 <args>` runs the real client as that user against this socket ---
write_wrapper() {
  local user="$1"
  cat > "$BIN/envh-$user" <<EOF
#!/usr/bin/env bash
# Run the real envh client as $user against the arena socket (override with ENVH_SOCKET).
exec sudo -u $user env ENVH_SOCKET="\${ENVH_SOCKET:-$SOCKET}" "$VENV/bin/python" -m envh "\$@"
EOF
  chmod 0755 "$BIN/envh-$user"
}
for u in "${LISTED_USERS[@]}" "$UNLISTED_USER"; do write_wrapper "$u"; done

# --- per-instance data dir, owned by the service user 0700 (attackers cannot read it) -------
if [[ -S "$SOCKET" ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null || echo -1)" 2>/dev/null; then
  say "instance '$INSTANCE' already running on $SOCKET (pid $(cat "$PIDFILE"))"; exit 0
fi
mkdir -p "$DATA"
rm -f "$DATA"/vault.age "$DATA"/config.yaml "$DATA"/presets.yaml "$DATA"/audit.jsonl 2>/dev/null || true
chown -R "$SVC_USER":"$SVC_USER" "$(dirname "$DATA")"; chmod 700 "$DATA"

PASS="$("$PY" -c 'import secrets;print(secrets.token_urlsafe(24))')"

# --- init the vault as the service user; capture PASS/FLAG/CANARY (kept 0600 root) ----------
say "initializing vault for instance '$INSTANCE'"
INIT_OUT="$(sudo -u "$SVC_USER" env \
  ENVH_ARENA_DATA="$DATA" ENVH_ARENA_PASS="$PASS" ENVH_ARENA_USERS="$(IFS=,; echo "${LISTED_USERS[*]}")" \
  "$VENV/bin/python" "$SCRIPTS/init_vault.py")"
umask 077
{ echo "$INIT_OUT"; echo "PASS=$PASS"; echo "INSTANCE=$INSTANCE"; echo "SOCKET=$SOCKET"; } > "$SECRETS_FILE"
chmod 600 "$SECRETS_FILE"

# --- launch the broker, detached, as the service user ---------------------------------------
say "launching headless broker"
setsid sudo -u "$SVC_USER" env \
  ENVH_ARENA_DATA="$DATA" ENVH_ARENA_SOCKET="$SOCKET" ENVH_ARENA_PASS="$PASS" \
  bash -c 'echo $$ > "'"$PIDFILE"'"; exec "'"$VENV"'/bin/python" "'"$SCRIPTS"'/serve_broker.py"' \
  >"$LOG" 2>&1 < /dev/null &
disown || true

# --- health check: the socket comes up and a listed user gets a clean status reply ----------
for _ in $(seq 1 50); do [[ -S "$SOCKET" ]] && break; sleep 0.2; done
if [[ ! -S "$SOCKET" ]]; then echo "broker did not create the socket; see $LOG"; tail -20 "$LOG" || true; exit 1; fi
if ! sudo -u "${LISTED_USERS[0]}" env ENVH_SOCKET="$SOCKET" "$VENV/bin/python" -m envh status >/dev/null 2>&1; then
  echo "broker did not answer a status from ${LISTED_USERS[0]}; see $LOG"; tail -20 "$LOG" || true; exit 1
fi

say "ready."
echo "  instance : $INSTANCE"
echo "  socket   : $SOCKET"
echo "  listed   : ${LISTED_USERS[*]}   unlisted: $UNLISTED_USER   service: $SVC_USER"
echo "  clients  : $BIN/envh-rt1  $BIN/envh-rt2  $BIN/envh-outsider   (set ENVH_SOCKET for other instances)"
echo "  raw NDJSON: sudo -u rt1 env ENVH_SOCKET=$SOCKET $VENV/bin/python $SCRIPTS/raw_client.py '{\"op\":\"status\"}'"
echo "  secrets  : $SECRETS_FILE (0600 root; holds FLAG/CANARY/PASS for graders)"
echo "  fingerprint:"
ENVH_ARENA_REPO="$REPO" "$VENV/bin/python" "$SCRIPTS/fingerprint.py" | sed 's/^/    /'
