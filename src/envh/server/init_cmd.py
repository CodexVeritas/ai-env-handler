"""`envh init`: create the data directory contents for a fresh installation."""

from __future__ import annotations

import argparse
import getpass
import os
import pwd
import secrets
from pathlib import Path

from envh.platform import PlatformError, service_user_home
from envh.core.config import CONFIG_FILE, PRESETS_FILE, PRESETS_TEMPLATE, render_config_template
from envh.core.vault import MIN_PASSPHRASE_LENGTH, VAULT_FILE, Vault, write_private_file

PHRASE_FILE = "console-phrase"
WORDS = (
    "amber", "basil", "cedar", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper",
    "kestrel", "lantern", "marble", "nectar", "orchid", "pepper", "quartz", "raven", "saffron", "tundra",
    "umber", "velvet", "willow", "yonder", "zephyr", "anchor", "birch", "copper", "dune", "falcon",
)


def default_data_dir() -> Path:
    try:
        return service_user_home()
    except PlatformError:
        return Path.home() / ".envh"


def prompt_new_secret(label: str) -> str:
    while True:
        first = getpass.getpass(f"  {label}: ")
        if len(first) < MIN_PASSPHRASE_LENGTH:
            print(f"  Use at least {MIN_PASSPHRASE_LENGTH} characters.")
            continue
        second = getpass.getpass("  Repeat it: ")
        if first == second:
            return first
        print("  They don't match. Try again.")


def default_user() -> str:
    return os.environ.get("ENVH_INSTALL_USER") or os.environ.get("SUDO_USER") or getpass.getuser()


def run_init(data_dir: Path, user: str) -> None:
    try:
        pwd.getpwnam(user)
    except KeyError as error:
        raise SystemExit(f"no such login name {user!r}; pass --user") from error
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    config_path = data_dir / CONFIG_FILE
    presets_path = data_dir / PRESETS_FILE
    vault_path = data_dir / VAULT_FILE
    phrase_path = data_dir / PHRASE_FILE
    if not config_path.exists():
        write_private_file(config_path, render_config_template(user).encode())
    if not presets_path.exists():
        write_private_file(presets_path, PRESETS_TEMPLATE.encode())
    if not vault_path.exists():
        print()
        print("  Choose a vault passphrase. You'll type it to approve each key request, and it can't be recovered.")
        Vault.create(vault_path, prompt_new_secret("Passphrase"))
        print("  ✓ Vault created")
    if not phrase_path.exists():
        phrase = " ".join(secrets.choice(WORDS) for _ in range(3))
        write_private_file(phrase_path, (phrase + "\n").encode())
    phrase = phrase_path.read_text().strip()
    print()
    print(f"  Console phrase: {phrase}")
    print("  The real console always shows it. Never type your passphrase where it's missing.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="envh init", description="Create config, presets, vault and console phrase")
    parser.add_argument("--data-dir", type=Path, default=None, help="defaults to the envh service user's home directory")
    parser.add_argument("--user", default=None, help="login name allowed to talk to the broker (default: the user who ran sudo, else you)")
    args = parser.parse_args(argv)
    run_init(args.data_dir or default_data_dir(), args.user or default_user())
    return 0
