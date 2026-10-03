"""The vault: a flat JSON object of secrets, encrypted with age in passphrase mode."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

import pyrage
import pyrage.passphrase

VAULT_FILE = "vault.age"
MIN_PASSPHRASE_LENGTH = 8


class VaultError(RuntimeError):
    pass


def encrypt_secrets(secrets: dict[str, str], passphrase: str) -> bytes:
    payload = json.dumps(secrets, sort_keys=True, indent=1).encode()
    return pyrage.passphrase.encrypt(payload, passphrase)


def decrypt_secrets(blob: bytes, passphrase: str) -> dict[str, str]:
    try:
        payload = pyrage.passphrase.decrypt(blob, passphrase)
    except pyrage.DecryptError as error:
        raise VaultError("wrong passphrase or corrupted vault") from error
    secrets = json.loads(payload)
    if not isinstance(secrets, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in secrets.items()):
        raise VaultError("vault content is not a flat object of strings")
    return secrets


def write_private_file(path: Path, data: bytes) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.unlink(missing_ok=True)
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


class Vault:
    def __init__(self, path: Path, passphrase: str, secrets: dict[str, str]) -> None:
        self._path = path
        self._passphrase = passphrase
        self._secrets = dict(secrets)

    @classmethod
    def open(cls, path: Path, passphrase: str) -> "Vault":
        if not path.exists():
            raise VaultError(f"{path} does not exist; run `envh init`")
        return cls(path, passphrase, decrypt_secrets(path.read_bytes(), passphrase))

    @classmethod
    def create(cls, path: Path, passphrase: str) -> "Vault":
        if path.exists():
            raise VaultError(f"{path} already exists")
        vault = cls(path, passphrase, {})
        vault.save()
        return vault

    @property
    def path(self) -> Path:
        return self._path

    def names(self) -> list[str]:
        return sorted(self._secrets)

    def __contains__(self, name: str) -> bool:
        return name in self._secrets

    def get(self, name: str) -> str:
        try:
            return self._secrets[name]
        except KeyError as error:
            raise VaultError(f"secret {name} is not in the vault") from error

    def set(self, name: str, value: str) -> None:
        self._secrets[name] = value

    def remove(self, name: str) -> None:
        if name not in self._secrets:
            raise VaultError(f"secret {name} is not in the vault")
        del self._secrets[name]

    def matches_passphrase(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self._passphrase.encode())

    def change_passphrase(self, new_passphrase: str) -> None:
        self._passphrase = new_passphrase
        self.save()

    def save(self) -> None:
        write_private_file(self._path, encrypt_secrets(self._secrets, self._passphrase))
