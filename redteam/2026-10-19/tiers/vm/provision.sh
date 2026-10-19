#!/usr/bin/env bash
# Tier 2 VM provisioner: install toolchain + a venv with envh, then hand off to gate_tests.sh.
# Runs as root inside the guest.
set -euo pipefail
REPO=/vagrant
VENV=/opt/arena-venv

export DEBIAN_FRONTEND=noninteractive
(command -v apt-get && apt-get update && apt-get install -y python3 python3-venv sudo curl git) || \
(command -v dnf && dnf install -y python3 sudo curl git) || true

command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv; }
[[ -x "$VENV/bin/python" ]] || { uv venv "$VENV" --python 3.12; uv pip install --python "$VENV/bin/python" "$REPO"; }

echo "guest kernel: $(uname -r)"
echo "legacy_tiocsti control present: $([[ -e /proc/sys/dev/tty/legacy_tiocsti ]] && echo yes || echo no)"
echo "yama node present: $([[ -e /proc/sys/kernel/yama/ptrace_scope ]] && echo yes || echo no)"
echo "Run the gate tests with:  sudo bash $REPO/redteam/2026-10-19/tiers/vm/gate_tests.sh $VENV"
