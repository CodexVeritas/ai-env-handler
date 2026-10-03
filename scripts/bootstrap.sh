#!/bin/sh
# Install envh system-wide. Run from the repository root with:  sudo scripts/bootstrap.sh [--disable-sudo-cache]
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo scripts/bootstrap.sh" >&2
    exit 1
fi
repo=$(cd "$(dirname "$0")/.." && pwd)
invoker=${SUDO_USER:-root}
invoker_home=$(getent passwd "$invoker" | cut -d: -f6)
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$invoker_home/.local/bin/uv" ]; then
    uv_bin="$invoker_home/.local/bin/uv"
fi
if [ -z "$uv_bin" ]; then
    echo "uv not found. Install it as your normal user first:  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
echo "copying the repository to a staging directory (no editable install: root runs only what it copied)"
tar --exclude=.venv --exclude=.git --exclude=temp -C "$repo" -cf - . | tar -C "$staging" -xf -
echo "creating /opt/envh with Python 3.12"
"$uv_bin" venv /opt/envh --python 3.12 --clear --quiet
"$uv_bin" pip install --python /opt/envh/bin/python --quiet "$staging"
chown -R root:root /opt/envh
chmod -R go-w /opt/envh
ln -sf /opt/envh/bin/envh /usr/local/bin/envh
echo "running envh install"
exec /opt/envh/bin/envh install "$@"
