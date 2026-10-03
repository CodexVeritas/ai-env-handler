"""The approval password: kept only as a scrypt hash, checked on the console before anything is approved or changed."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

PASSWORD_FILE = "approval-password"
MIN_PASSWORD_LENGTH = 8
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


class PasswordError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{salt.hex()}:{key.hex()}"


def parse_stored(stored: str) -> tuple[int, int, int, bytes, bytes]:
    parts = stored.strip().split(":")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise PasswordError("the stored approval password is malformed")
    try:
        return int(parts[1]), int(parts[2]), int(parts[3]), bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError as error:
        raise PasswordError("the stored approval password is malformed") from error


def verify_password(password: str, stored: str) -> bool:
    n, r, p, salt, expected = parse_stored(stored)
    actual = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def load_password_hash(data_dir: Path) -> str:
    path = data_dir / PASSWORD_FILE
    try:
        stored = path.read_text().strip()
    except FileNotFoundError as error:
        raise PasswordError(f"no approval password is set ({path})") from error
    parse_stored(stored)
    return stored
