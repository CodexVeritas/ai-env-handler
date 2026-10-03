from pathlib import Path

import pytest

from envh.core.password import PASSWORD_FILE, PasswordError, hash_password, load_password_hash, verify_password
from envh.server import init_cmd
from tests.conftest import ME


def test_hash_verifies_only_the_right_password_and_is_salted() -> None:
    stored = hash_password("correct horse")
    assert verify_password("correct horse", stored) and not verify_password("wrong horse", stored)
    assert stored != hash_password("correct horse") and "correct horse" not in stored


def test_load_refuses_a_missing_or_malformed_password_file(tmp_path: Path) -> None:
    with pytest.raises(PasswordError, match="no approval password"):
        load_password_hash(tmp_path)
    (tmp_path / PASSWORD_FILE).write_text("plaintext-password\n")
    with pytest.raises(PasswordError, match="malformed"):
        load_password_hash(tmp_path)
    stored = hash_password("correct horse")
    (tmp_path / PASSWORD_FILE).write_text(stored + "\n")
    assert load_password_hash(tmp_path) == stored


def test_init_sets_the_approval_password_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["vault-passphrase", "vault-passphrase", "approval-password", "approval-password"])
    monkeypatch.setattr(init_cmd.getpass, "getpass", lambda prompt: next(answers))
    init_cmd.run_init(tmp_path, ME)
    stored = (tmp_path / PASSWORD_FILE).read_text()
    assert verify_password("approval-password", stored) and "approval-password" not in stored
    init_cmd.run_init(tmp_path, ME)
    assert (tmp_path / PASSWORD_FILE).read_text() == stored
