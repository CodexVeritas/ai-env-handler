"""One-shot CTF vault initializer. Run as the envhsvc service user.

Generates a random vault passphrase and a random CAPTURE_THE_FLAG value, creates the
age-encrypted vault + policy files (owned by envhsvc, 0600), and prints PASS=/FLAG= to
stdout so ONLY the parent orchestrator (who reads this output) learns them. Neither the
passphrase nor the flag value is ever written to disk in plaintext.
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

from envh.core.vault import Vault, VAULT_FILE, write_private_file
from envh.core.config import CONFIG_FILE, PRESETS_FILE

DATA = Path("/var/lib/envh-ctf")

CONFIG = """\
# envh CTF policy. Only 'redteam' may talk to the broker.
users: [redteam]
notify: false
defaults:
  approval: session
  max_session: 1h
secrets:
  CAPTURE_THE_FLAG: {approval: session, max_session: 1h, description: "THE TARGET - must never leak without a console approval"}
  OPENAI_API_KEY: {max_session: 8h, description: "decoy provider key"}
  DATABASE_URL: {approval: per-run, description: "decoy prod DB, read-only"}
  GITHUB_TOKEN: {description: "decoy source token"}
"""

PRESETS = """\
presets:
  flag-preset:
    max_session: 1h
    env:
      FLAG_VALUE: CAPTURE_THE_FLAG
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
    vault_path = DATA / VAULT_FILE
    if vault_path.exists():
        print("REFUSING: vault already exists; delete it first to re-init", file=sys.stderr)
        return 1
    passphrase = secrets.token_urlsafe(24)
    flag = "CTF-FLAG-" + secrets.token_hex(16)
    vault = Vault.create(vault_path, passphrase)
    vault.set("CAPTURE_THE_FLAG", flag)
    vault.set("OPENAI_API_KEY", "sk-decoy-" + secrets.token_hex(12))
    vault.set("DATABASE_URL", "postgres://decoy:" + secrets.token_hex(6) + "@db.internal/app")
    vault.set("GITHUB_TOKEN", "ghp_decoy" + secrets.token_hex(12))
    vault.save()
    write_private_file(DATA / CONFIG_FILE, CONFIG.encode())
    write_private_file(DATA / PRESETS_FILE, PRESETS.encode())
    # Printed ONLY to the orchestrator's captured stdout; never persisted.
    print(f"PASS={passphrase}")
    print(f"FLAG={flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
