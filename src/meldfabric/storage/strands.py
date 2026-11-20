"""SQL-backed Strands storage for sessions, snapshots, and plugin state."""

from __future__ import annotations

import builtins
import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, LargeBinary, String, Text, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from meldfabric.storage.database import AsyncDatabase


class StrandsStorageBase(DeclarativeBase):
    """Tables installed in user-selected runtime databases."""


class StrandsStorageRecord(StrandsStorageBase):
    """Opaque Strands bytes keyed within a MeldFabric-owned logical namespace."""

    __tablename__ = "meldfabric_strands_storage"

    namespace: Mapped[str] = mapped_column(String(255), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class StrandsMemoryRecord(StrandsStorageBase):
    """One idempotent long-term memory entry in an isolated MeldFabric scope."""

    __tablename__ = "meldfabric_strands_memory"

    namespace: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    entry_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    entry_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SQLStorage:
    """Implement the Strands ``Storage`` protocol on a MeldFabric database resource.

    Strands intentionally treats keys and payloads as opaque.  Keeping one compact
    table lets sessions, context offloading, plugin state, and future SDK features use
    the same database resource without coupling this library to their JSON formats.
    The database-level thread lease remains the single-writer guarantee for a live
    conversation; this class only makes individual writes transactional.
    """

    def __init__(self, database: AsyncDatabase, *, namespace: str) -> None:
        if not namespace or len(namespace) > 255:
            raise ValueError("Strands SQL storage namespace must contain 1-255 characters")
        self.database = database
        self.namespace = namespace
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self.database.engine.begin() as connection:
            await connection.run_sync(StrandsStorageBase.metadata.create_all)
        self._initialized = True

    async def write(self, key: str, data: bytes) -> None:
        self._validate_key(key)
        if not isinstance(data, bytes):
            raise TypeError("Strands storage payload must be bytes")
        await self.initialize()
        digest = self._key_hash(key)
        async with self.database.session() as session, session.begin():
            record = await session.get(StrandsStorageRecord, (self.namespace, digest))
            if record is None:
                session.add(
                    StrandsStorageRecord(
                        namespace=self.namespace,
                        key_hash=digest,
                        storage_key=key,
                        payload=data,
                    )
                )
            else:
                self._verify_key(record, key)
                record.payload = data

    async def read(self, key: str) -> bytes | None:
        self._validate_key(key)
        await self.initialize()
        async with self.database.session() as session:
            record = await session.get(
                StrandsStorageRecord,
                (self.namespace, self._key_hash(key)),
            )
            if record is None:
                return None
            self._verify_key(record, key)
            return bytes(record.payload)

    async def delete(self, key: str) -> None:
        self._validate_key(key)
        await self.initialize()
        async with self.database.session() as session, session.begin():
            record = await session.get(
                StrandsStorageRecord,
                (self.namespace, self._key_hash(key)),
            )
            if record is not None:
                self._verify_key(record, key)
                await session.delete(record)

    async def list(self, query: str = "") -> list[str]:
        if not isinstance(query, str):
            raise TypeError("Strands SQL storage list query must be a string prefix")
        await self.initialize()
        async with self.database.session() as session:
            keys = (
                await session.scalars(
                    select(StrandsStorageRecord.storage_key).where(
                        StrandsStorageRecord.namespace == self.namespace
                    )
                )
            ).all()
        return sorted(key for key in keys if key.startswith(query))

    async def search(self, query: str) -> builtins.list[Any]:
        # Use the SDK's stable keyword strategy rather than duplicating its scoring.
        from strands.storage.search import KeywordSearchStrategy

        return await KeywordSearchStrategy().search(self, query)

    async def clear(self) -> None:
        """Remove this namespace without touching unrelated MeldFabric data."""

        await self.initialize()
        async with self.database.session() as session, session.begin():
            await session.execute(
                delete(StrandsStorageRecord).where(StrandsStorageRecord.namespace == self.namespace)
            )

    async def delete_prefix(self, prefix: str) -> int:
        """Delete keys under one SDK-owned scope and return the removed count."""

        if not prefix:
            raise ValueError("Strands storage delete prefix must not be empty")
        keys = await self.list(prefix)
        for key in keys:
            await self.delete(key)
        return len(keys)

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("Strands storage key must be a non-empty string")

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _verify_key(record: StrandsStorageRecord, key: str) -> None:
        if record.storage_key != key:
            raise RuntimeError("SHA-256 collision detected in Strands SQL storage")
