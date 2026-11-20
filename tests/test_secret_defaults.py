from __future__ import annotations

import hashlib
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from meldfabric.security import SecretCipher
from meldfabric.security.crypto import MasterKeyUnavailableError


def _worker_ciphertext(key_file: Path) -> bytes:
    cipher = SecretCipher.from_environment("TEST_DEFAULT_MASTER_KEY", default_path=key_file)
    return cipher.encrypt("worker-credential", associated_data="test")


def test_default_master_key_is_shared_by_concurrent_workers(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DEFAULT_MASTER_KEY", raising=False)
    key_file = tmp_path / "keys" / "workspace.key"
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        ciphertexts = list(pool.map(_worker_ciphertext, [key_file] * 8))
    cipher = SecretCipher.from_environment("TEST_DEFAULT_MASTER_KEY", default_path=key_file)
    assert all(
        cipher.decrypt(value, associated_data="test") == "worker-credential"
        for value in ciphertexts
    )
    if os.name == "posix":
        assert key_file.stat().st_mode & 0o777 == 0o600
    other = SecretCipher.from_environment(
        "TEST_DEFAULT_MASTER_KEY", default_path=tmp_path / "other-workspace.key"
    )
    assert other.key != cipher.key


def test_explicit_environment_overrides_default_without_overwriting_it(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DEFAULT_MASTER_KEY", raising=False)
    key_file = tmp_path / "workspace.key"
    original = SecretCipher.from_environment("TEST_DEFAULT_MASTER_KEY", default_path=key_file)
    contents = key_file.read_bytes()
    monkeypatch.setenv("TEST_DEFAULT_MASTER_KEY", "explicit-test-master")
    override = SecretCipher.from_environment("TEST_DEFAULT_MASTER_KEY", default_path=key_file)
    assert override.key == hashlib.sha256(b"explicit-test-master").digest()
    assert override.key != original.key
    assert key_file.read_bytes() == contents


def test_invalid_default_key_is_not_silently_replaced(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_DEFAULT_MASTER_KEY", raising=False)
    key_file = tmp_path / "workspace.key"
    key_file.write_text("damaged")
    with pytest.raises(MasterKeyUnavailableError) as caught:
        SecretCipher.from_environment("TEST_DEFAULT_MASTER_KEY", default_path=key_file)
    assert caught.value.details["action"] == "restore_key_file_or_fix_permissions"
    assert key_file.read_text() == "damaged"
