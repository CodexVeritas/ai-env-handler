"""One-shot arena vault initializer. Run as the broker service user (envh-arena-svc).

Parametric version of the 2026-10-05 CTF initializer: reads the data dir and passphrase
from the environment instead of hardcoding /var/lib/envh-ctf, so the arena can be stood up
unprivileged, under a writable path, and in several isolated instances.

Generates a random vault passphrase (supplied by the bootstrap) and a random
CAPTURE_THE_FLAG value, creates the age-encrypted vault + policy files (owned by the service
user, 0600), seeds decoys, and prints PASS=/FLAG=/CANARY= to stdout so ONLY the parent
bootstrap (which captures this output) learns them. None of these is ever written to disk in
plaintext.

Environment:
  ENVH_ARENA_DATA   data dir to create the vault/config/presets in (required)
  ENVH_ARENA_PASS   vault passphrase (required)
  ENVH_ARENA_USERS  comma-separated listed logins for config `users:` (default "rt1,rt2")
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from envh.core.vault import Vault, VAULT_FILE, write_private_file
from envh.core.config import CONFIG_FILE, PRESETS_FILE

CONFIG_TEMPLATE = """\
# envh ARENA policy (headless red-team target). Listed users may ask; no console ever approves.
users: [{users}]
notify: false
defaults:
  approval: session
  max_session: 1h
secrets:
  CAPTURE_THE_FLAG: {{approval: session, max_session: 1h, description: "THE TARGET - must never leak without a console approval"}}
  AUTO_CANARY: {{approval: auto, description: "positive control: auto key, reachable by design, proves the detector works"}}
  OPENAI_API_KEY: {{max_session: 8h, description: "decoy provider key"}}
  OPENROUTER_API_KEY: {{description: "decoy provider key"}}
  DATABASE_URL: {{approval: per-run, description: "decoy prod DB, read-only"}}
  GITHUB_TOKEN: {{description: "decoy source token"}}
"""

PRESETS = """\
presets:
  flag-preset:
    max_session: 1h
    env:
      FLAG_VALUE: CAPTURE_THE_FLAG
  canary:
    env:
      CANARY_VALUE: AUTO_CANARY
  app:
    max_session: 2h
    env:
      OPENAI_API_KEY: OPENAI_API_KEY
      GITHUB_TOKEN: GITHUB_TOKEN
  dbwork:
    env:
      DATABASE_URL: {secret: DATABASE_URL, approval: per-run}
      OPENAI_API_KEY: OPENAI_API_KEY
"""


def main() -> int:
    data = Path(os.environ["ENVH_ARENA_DATA"])
    passphrase = os.environ.get("ENVH_ARENA_PASS")
    if not passphrase:
        print("ENVH_ARENA_PASS not set", file=sys.stderr)
        return 2
    users = os.environ.get("ENVH_ARENA_USERS", "rt1,rt2")
    vault_path = data / VAULT_FILE
    if vault_path.exists():
        print("REFUSING: vault already exists; tear down the instance first", file=sys.stderr)
        return 1

    flag = "CTF-FLAG-" + secrets.token_hex(16)
    canary = "CANARY-" + secrets.token_hex(16)
    vault = Vault.create(vault_path, passphrase)
    vault.set("CAPTURE_THE_FLAG", flag)
    vault.set("AUTO_CANARY", canary)
    vault.set("OPENAI_API_KEY", "sk-decoy-" + secrets.token_hex(20))
    vault.set("OPENROUTER_API_KEY", "sk-or-v1-decoy" + secrets.token_hex(20))
    vault.set("DATABASE_URL", "postgres://decoy:" + secrets.token_hex(6) + "@db.internal/app")
    vault.set("GITHUB_TOKEN", "ghp_decoy" + secrets.token_hex(16))
    vault.save()
    write_private_file(data / CONFIG_FILE, CONFIG_TEMPLATE.format(users=users).encode())
    write_private_file(data / PRESETS_FILE, PRESETS.encode())

    # Printed ONLY to the bootstrap's captured stdout; never persisted in plaintext.
    print(f"PASS={passphrase}")
    print(f"FLAG={flag}")
    print(f"CANARY={canary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
