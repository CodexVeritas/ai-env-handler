import json
import os
import stat
from pathlib import Path

import pyrage.passphrase
import pytest

from envh.core.vault import Vault, VaultError, decrypt_secrets, encrypt_secrets


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "vault.age"
    vault = Vault.create(path, "pw")
    vault.set("OPENAI_API_KEY", "sk-test")
    vault.save()
    reopened = Vault.open(path, "pw")
    assert reopened.get("OPENAI_API_KEY") == "sk-test"
    assert reopened.names() == ["OPENAI_API_KEY"]
    assert "OPENAI_API_KEY" in reopened


def test_wrong_passphrase(tmp_path: Path) -> None:
    path = tmp_path / "vault.age"
    Vault.create(path, "pw")
    with pytest.raises(VaultError, match="wrong passphrase"):
        Vault.open(path, "other")


def test_file_mode_and_no_temp_left(tmp_path: Path) -> None:
    path = tmp_path / "vault.age"
    Vault.create(path, "pw")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert sorted(child.name for child in tmp_path.iterdir()) == ["vault.age"]


def test_create_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "vault.age"
    Vault.create(path, "pw")
    with pytest.raises(VaultError, match="already exists"):
        Vault.create(path, "pw")


def test_remove_unknown(tmp_path: Path) -> None:
    vault = Vault.create(tmp_path / "vault.age", "pw")
    with pytest.raises(VaultError):
        vault.remove("NOPE")
    with pytest.raises(VaultError):
        vault.get("NOPE")


def test_rejects_non_flat_content() -> None:
    blob = pyrage.passphrase.encrypt(json.dumps({"A": {"nested": 1}}).encode(), "pw")
    with pytest.raises(VaultError, match="flat object"):
        decrypt_secrets(blob, "pw")
    assert decrypt_secrets(encrypt_secrets({"A": "b"}, "pw"), "pw") == {"A": "b"}


def test_stale_temp_file_does_not_block_saves(tmp_path: Path) -> None:
    path = tmp_path / "vault.age"
    (tmp_path / "vault.age.tmp").write_text("leftover")
    Vault.create(path, "pw")
    assert not (tmp_path / "vault.age.tmp").exists()
