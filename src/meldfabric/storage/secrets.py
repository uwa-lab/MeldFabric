"""Database-backed inbound key hashes and encrypted outbound credentials."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, or_, select, update

from meldfabric.config.schema import ControlGenerationSecretRef, ControlSecretVersion
from meldfabric.config.types import EncryptedSecretWrite
from meldfabric.security import SecretCipher, hash_inbound_key, verify_inbound_key
from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.schema import SecretRecord
from meldfabric.storage.sql import create_mapped_table


@dataclass(frozen=True, slots=True)
class CreatedInboundKey:
    id: str
    value: str
    prefix: str


@dataclass(frozen=True, slots=True)
class VerifiedInboundKey:
    id: str
    kind: str
    key_prefix: str | None
    agent_id: str | None
    scopes: tuple[str, ...]
    status: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class _CachedVerification:
    record: VerifiedInboundKey
    valid_until: float


class SecretReader(Protocol):
    """Minimal credential access required by revision-scoped runtime consumers."""

    async def get_secret(self, secret_id: str) -> str | None: ...


class SecretRepository:
    verification_cache_ttl_seconds = 60.0
    verification_cache_max_entries = 4096

    def __init__(self, database: AsyncDatabase, cipher: SecretCipher) -> None:
        self.database = database
        self.cipher = cipher
        self._cache_lock = asyncio.Lock()
        self._verified_keys: OrderedDict[bytes, _CachedVerification] = OrderedDict()
        self._pending_key_usage: dict[str, datetime] = {}

    async def initialize(self) -> None:
        async with self.database.engine.begin() as connection:
            await create_mapped_table(connection, ControlSecretVersion)
            await create_mapped_table(connection, SecretRecord)

    async def put_secret(self, secret_id: str, *, kind: str, value: str) -> None:
        associated_data = f"{secret_id}:{kind}"
        encrypted = self.cipher.encrypt(value, associated_data=associated_data)
        async with self.database.session() as session, session.begin():
            current_version = await session.scalar(
                select(func.max(ControlSecretVersion.version)).where(
                    ControlSecretVersion.secret_id == secret_id
                )
            )
            session.add(
                ControlSecretVersion(
                    secret_id=secret_id,
                    version=int(current_version or 0) + 1,
                    kind=kind,
                    encrypted_value=encrypted,
                )
            )
            record = await session.get(SecretRecord, secret_id)
            if record is None:
                session.add(
                    SecretRecord(id=secret_id, kind=kind, encrypted_value=encrypted, scopes=[])
                )
            else:
                record.kind = kind
                record.encrypted_value = encrypted
                record.key_hash = None
                record.status = "active"

    def prepare_secret_write(
        self,
        secret_id: str,
        *,
        kind: str,
        value: str,
    ) -> EncryptedSecretWrite:
        """Encrypt a value for the control repository's atomic generation transaction."""

        return EncryptedSecretWrite(
            secret_id=secret_id,
            kind=kind,
            encrypted_value=self.cipher.encrypt(value, associated_data=f"{secret_id}:{kind}"),
        )

    async def get_secret(self, secret_id: str) -> str | None:
        async with self.database.session() as session:
            record = await session.get(SecretRecord, secret_id)
            if record is None or record.status != "active" or record.encrypted_value is None:
                return None
            return self.cipher.decrypt(
                record.encrypted_value,
                associated_data=f"{record.id}:{record.kind}",
            )

    async def get_secret_for_generation(
        self,
        secret_id: str,
        *,
        workspace_id: str,
        generation: int,
    ) -> str | None:
        """Read the immutable credential version pinned by one config generation."""

        async with self.database.session() as session:
            reference = await session.get(
                ControlGenerationSecretRef,
                (workspace_id, generation, secret_id),
            )
            if reference is None:
                return None
            version = await session.get(
                ControlSecretVersion,
                (secret_id, reference.secret_version),
            )
            if version is None:
                return None
            return self.cipher.decrypt(
                version.encrypted_value,
                associated_data=f"{secret_id}:{version.kind}",
            )

    async def delete_secret(self, secret_id: str) -> bool:
        async with self.database.session() as session, session.begin():
            record = await session.get(SecretRecord, secret_id)
            if record is None:
                return False
            await session.delete(record)
        await self._invalidate_key_cache(secret_id)
        return True

    async def create_inbound_key(
        self,
        *,
        kind: str,
        agent_id: str | None,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> CreatedInboundKey:
        key_id = uuid.uuid4().hex
        kind_prefix = "rak" if kind == "remote_access" else "aak"
        value = f"meldfabric_{kind_prefix}_{key_id}_{secrets.token_urlsafe(32)}"
        visible_prefix = value[:24]
        key_hash = await asyncio.to_thread(hash_inbound_key, value)
        async with self.database.session() as session, session.begin():
            session.add(
                SecretRecord(
                    id=key_id,
                    kind=kind,
                    key_hash=key_hash,
                    key_prefix=visible_prefix,
                    agent_id=agent_id,
                    scopes=scopes,
                    status="active",
                    expires_at=expires_at,
                )
            )
        return CreatedInboundKey(id=key_id, value=value, prefix=visible_prefix)

    async def verify_key(
        self,
        value: str,
        *,
        required_scope: str | None = None,
    ) -> VerifiedInboundKey | None:
        parts = value.split("_", 3)
        if len(parts) != 4 or parts[0] != "meldfabric":
            return None
        key_id = parts[2]
        token_digest = hashlib.sha256(value.encode("utf-8")).digest()
        now = datetime.now(UTC)
        cached = await self._cached_verification(token_digest, now=now)
        if cached is not None:
            if required_scope is not None and required_scope not in cached.scopes:
                return None
            await self._record_key_usage(cached.id, now)
            return cached

        async with self.database.session() as session:
            record = await session.get(SecretRecord, key_id)
            if record is None or record.status != "active" or not record.key_hash:
                return None
            expires_at = record.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    return None
            if required_scope is not None and required_scope not in record.scopes:
                return None
            key_hash = record.key_hash
            verified = VerifiedInboundKey(
                id=record.id,
                kind=record.kind,
                key_prefix=record.key_prefix,
                agent_id=record.agent_id,
                scopes=tuple(record.scopes),
                status=record.status,
                expires_at=expires_at,
            )
        if not await asyncio.to_thread(verify_inbound_key, value, key_hash):
            return None
        await self._cache_verification(token_digest, verified)
        await self._record_key_usage(verified.id, now)
        return verified

    async def flush_key_usage(self) -> int:
        """Persist coalesced last-used timestamps collected by cached verification."""

        async with self._cache_lock:
            pending = self._pending_key_usage
            self._pending_key_usage = {}
        if not pending:
            return 0
        try:
            async with self.database.session() as session, session.begin():
                for key_id, used_at in pending.items():
                    await session.execute(
                        update(SecretRecord)
                        .where(SecretRecord.id == key_id)
                        .where(
                            or_(
                                SecretRecord.last_used_at.is_(None),
                                SecretRecord.last_used_at < used_at,
                            )
                        )
                        .values(last_used_at=used_at)
                    )
        except Exception:
            async with self._cache_lock:
                for key_id, used_at in pending.items():
                    previous = self._pending_key_usage.get(key_id)
                    if previous is None or used_at > previous:
                        self._pending_key_usage[key_id] = used_at
            raise
        return len(pending)

    async def _cached_verification(
        self,
        token_digest: bytes,
        *,
        now: datetime,
    ) -> VerifiedInboundKey | None:
        monotonic_now = time.monotonic()
        async with self._cache_lock:
            cached = self._verified_keys.get(token_digest)
            if cached is None:
                return None
            expires_at = cached.record.expires_at
            if cached.valid_until <= monotonic_now or (
                expires_at is not None and expires_at <= now
            ):
                self._verified_keys.pop(token_digest, None)
                return None
            self._verified_keys.move_to_end(token_digest)
            return cached.record

    async def _cache_verification(
        self,
        token_digest: bytes,
        record: VerifiedInboundKey,
    ) -> None:
        async with self._cache_lock:
            self._verified_keys[token_digest] = _CachedVerification(
                record=record,
                valid_until=time.monotonic() + self.verification_cache_ttl_seconds,
            )
            self._verified_keys.move_to_end(token_digest)
            while len(self._verified_keys) > self.verification_cache_max_entries:
                self._verified_keys.popitem(last=False)

    async def _record_key_usage(self, key_id: str, used_at: datetime) -> None:
        async with self._cache_lock:
            previous = self._pending_key_usage.get(key_id)
            if previous is None or used_at > previous:
                self._pending_key_usage[key_id] = used_at

    async def _invalidate_key_cache(self, key_id: str) -> None:
        async with self._cache_lock:
            for digest, cached in tuple(self._verified_keys.items()):
                if cached.record.id == key_id:
                    self._verified_keys.pop(digest, None)

    async def revoke_key(self, key_id: str) -> bool:
        async with self.database.session() as session, session.begin():
            record = await session.get(SecretRecord, key_id)
            if record is None:
                return False
            record.status = "revoked"
        await self._invalidate_key_cache(key_id)
        return True

    async def set_key_status(self, key_id: str, status: str) -> bool:
        if status not in {"active", "disabled", "revoked"}:
            raise ValueError(f"unsupported key status {status!r}")
        async with self.database.session() as session, session.begin():
            record = await session.get(SecretRecord, key_id)
            if record is None or record.key_hash is None:
                return False
            if record.status == "revoked" and status != "revoked":
                return False
            record.status = status
        await self._invalidate_key_cache(key_id)
        return True

    async def revoke_keys_for_agent(self, agent_id: str) -> int:
        records = await self.list_keys(agent_id=agent_id)
        count = 0
        async with self.database.session() as session, session.begin():
            for record in records:
                current = await session.get(SecretRecord, record.id)
                if current is not None and current.status != "revoked":
                    current.status = "revoked"
                    count += 1
        for record in records:
            await self._invalidate_key_cache(record.id)
        return count

    async def list_keys(
        self,
        *,
        agent_id: str | None = None,
        statuses: set[str] | None = None,
    ) -> list[SecretRecord]:
        statement = select(SecretRecord).where(SecretRecord.key_hash.is_not(None))
        if agent_id is not None:
            statement = statement.where(SecretRecord.agent_id == agent_id)
        if statuses is not None:
            statement = statement.where(SecretRecord.status.in_(statuses))
        statement = statement.order_by(SecretRecord.created_at.desc())
        async with self.database.session() as session:
            return list((await session.scalars(statement)).all())


class GenerationSecretRepository:
    """Read-only credential view pinned to an immutable runtime generation."""

    def __init__(
        self,
        repository: SecretRepository,
        *,
        workspace_id: str,
        generation: int,
    ) -> None:
        self._repository = repository
        self.workspace_id = workspace_id
        self.generation = generation

    async def get_secret(self, secret_id: str) -> str | None:
        return await self._repository.get_secret_for_generation(
            secret_id,
            workspace_id=self.workspace_id,
            generation=self.generation,
        )
