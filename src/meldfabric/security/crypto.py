"""Small, versioned cryptographic primitives for stored credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from filelock import FileLock, Timeout

from meldfabric.errors import AgentKitError


class MasterKeyUnavailableError(AgentKitError):
    code = "MASTER_KEY_UNAVAILABLE"


class SecretDecryptionError(AgentKitError):
    code = "SECRET_DECRYPTION_FAILED"


_CIPHER_VERSION = b"\x01"
_SCRYPT_VERSION = "scrypt-v1"


def _persistent_default_key(path: Path, variable_name: str) -> str:
    """Create one random key atomically, shared by all workers of a workspace."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(path) + ".lock", timeout=10):
            if path.exists():
                value = path.read_text(encoding="ascii").strip()
                if len(value) != 43 or len(base64.urlsafe_b64decode(value + "=")) != 32:
                    raise ValueError("invalid default master key file")
                return value

            value = secrets.token_urlsafe(32)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="ascii", dir=path.parent, delete=False
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(value + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary_path, 0o600)
                os.replace(temporary_path, path)
                if os.name == "posix":
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return value
    except (OSError, ValueError, Timeout) as exc:
        raise MasterKeyUnavailableError(
            "无法读取或创建默认加密主密钥。请检查密钥文件及目录权限。"
            "损坏的密钥文件需要从备份恢复。",
            details={
                "key_file": str(path),
                "environment_variable": variable_name,
                "action": "restore_key_file_or_fix_permissions",
            },
        ) from exc


@dataclass(frozen=True, slots=True)
class SecretCipher:
    """AES-256-GCM cipher derived from an explicit or persistent default master key."""

    key: bytes

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.key).hexdigest()

    @classmethod
    def from_environment(
        cls, variable_name: str, *, default_path: Path | None = None
    ) -> SecretCipher:
        value = os.getenv(variable_name)
        if not value and default_path is not None:
            value = _persistent_default_key(default_path, variable_name)
        if not value:
            raise MasterKeyUnavailableError(
                f"Cannot save credentials: environment variable {variable_name!r} is not set. "
                "Set a stable encryption master key in the service process environment "
                "and restart the service. This is separate from the administrator key.",
                details={
                    "environment_variable": variable_name,
                    "action": "set_environment_variable_and_restart",
                },
            )
        # Accept an operationally convenient passphrase while deriving a fixed-size key.
        # Deployments should still use a high-entropy random environment value.
        key = hashlib.sha256(value.encode("utf-8")).digest()
        return cls(key=key)

    def encrypt(self, plaintext: str, *, associated_data: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            associated_data.encode("utf-8"),
        )
        return _CIPHER_VERSION + nonce + ciphertext

    def decrypt(self, payload: bytes, *, associated_data: str) -> str:
        if not payload.startswith(_CIPHER_VERSION) or len(payload) < 30:
            raise SecretDecryptionError("stored secret has an unsupported or invalid format")
        nonce = payload[1:13]
        ciphertext = payload[13:]
        try:
            plaintext = AESGCM(self.key).decrypt(
                nonce,
                ciphertext,
                associated_data.encode("utf-8"),
            )
        except Exception as exc:
            raise SecretDecryptionError(
                "stored secret could not be decrypted with the configured master key"
            ) from exc
        return plaintext.decode("utf-8")


def hash_inbound_key(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return ":".join(
        [
            _SCRYPT_VERSION,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_inbound_key(value: str, encoded: str) -> bool:
    try:
        version, salt_encoded, digest_encoded = encoded.split(":", 2)
        if version != _SCRYPT_VERSION:
            return False
        salt = base64.urlsafe_b64decode(salt_encoded.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_encoded.encode("ascii"))
    except (ValueError, UnicodeError):
        return False
    actual = hashlib.scrypt(
        value.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)
