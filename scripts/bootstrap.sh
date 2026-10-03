#!/bin/sh
# Install envh system-wide. Run from the repository root with:  sudo scripts/bootstrap.sh [--dry-run] [--disable-sudo-cache]
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo "run with sudo: sudo scripts/bootstrap.sh" >&2
    exit 1
fi
dry_run=0
for arg in "$@"; do
    case "$arg" in --dry-run) dry_run=1 ;; esac
done
repo=$(cd "$(dirname "$0")/.." && pwd)
invoker=${SUDO_USER:-root}
invoker_home=$(getent passwd "$invoker" | cut -d: -f6)
uv_bin=$(command -v uv || true)
if [ -z "$uv_bin" ] && [ -x "$invoker_home/.local/bin/uv" ]; then
    uv_bin="$invoker_home/.local/bin/uv"
fi
if [ -z "$uv_bin" ]; then
    echo "uv not found. Install it first; a root-owned copy is safest, since this script runs it as root:  curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh" >&2
    exit 1
fi
staging=$(mktemp -d)
scratch=$(mktemp -d)
trap 'rm -rf "$staging" "$scratch"' EXIT
echo "copying the repository to a staging directory (no editable install: root runs only what it copied)"
tar --exclude=.venv --exclude=.git --exclude=temp -C "$repo" -cf - . | tar -C "$staging" -xf -
if [ "$dry_run" -eq 1 ]; then
    prefix="$scratch/envh"
    echo "dry run: would create /opt/envh with Python 3.12 and link /usr/local/bin/envh; building in $prefix instead so that envh install can print its plan"
else
    prefix=/opt/envh
    echo "creating /opt/envh with Python 3.12"
fi
"$uv_bin" venv "$prefix" --python 3.12 --clear --quiet
"$uv_bin" pip install --python "$prefix/bin/python" --quiet "$staging"
if [ "$dry_run" -eq 0 ]; then
    chown -R root:root /opt/envh
    chmod -R go-w /opt/envh
    ln -sf /opt/envh/bin/envh /usr/local/bin/envh
fi
echo "running envh install"
"$prefix/bin/envh" install "$@"
