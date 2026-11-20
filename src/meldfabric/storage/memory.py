"""Strands long-term memory backed by a MeldFabric database resource."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from strands.memory import ExtractionConfig, MemoryEntry, MemoryStore
from strands.memory.types import SearchOptions
from strands.types.tools import AgentTool

from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.sql import affected_rows
from meldfabric.storage.strands import StrandsMemoryRecord, StrandsStorageBase


def memory_namespace(workspace_id: str, agent_id: str, namespace: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join((workspace_id, agent_id, namespace)).encode("utf-8")
    ).hexdigest()[:32]
    return f"meldfabric_memory_{digest}"


class SQLMemoryStore(MemoryStore):
    """A scope-bound implementation of the Strands ``MemoryStore`` protocol.

    Instances are intentionally cheap and are created per Agent conversation. The
    database handle and schema are revision-owned, while ``scope_key`` prevents a
    caller from searching or writing another user's memories.

    Search is portable keyword ranking rather than pretending that every supported
    SQL engine provides vector search. Applications that need semantic retrieval can
    add another memory-store adapter in a future optional integration package.
    """

    writable = True
    extraction: ExtractionConfig | bool | None = False

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        namespace: str,
        scope_key: str,
        name: str = "long_term_memory",
        description: str | None = None,
        max_search_results: int = 5,
        schema_ready: bool = False,
    ) -> None:
        if not namespace or len(namespace) > 255:
            raise ValueError("memory namespace must contain 1-255 characters")
        if not scope_key:
            raise ValueError("memory scope key must not be empty")
        if max_search_results < 1:
            raise ValueError("max_search_results must be positive")
        self.database = database
        self.namespace = namespace
        self.scope_key = scope_key
        self.name = name
        self.description: str | None = (
            description or "Persistent facts and preferences for this user"
        )
        self.max_search_results = max_search_results
        self._scope_hash = self._digest(scope_key)
        self._initialized = schema_ready

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self.database.engine.begin() as connection:
            await connection.run_sync(StrandsStorageBase.metadata.create_all)
        self._initialized = True

    async def search(
        self,
        query: str,
        options: SearchOptions | None = None,
    ) -> list[MemoryEntry]:
        limit = int((options or {}).get("max_search_results") or self.max_search_results)
        limit = max(1, min(limit, 100))
        await self.initialize()
        async with self.database.session() as session:
            records = (
                await session.scalars(
                    select(StrandsMemoryRecord)
                    .where(
                        StrandsMemoryRecord.namespace == self.namespace,
                        StrandsMemoryRecord.scope_hash == self._scope_hash,
                    )
                    .order_by(StrandsMemoryRecord.updated_at.desc())
                    .limit(max(limit * 20, 200))
                )
            ).all()

        for record in records:
            self._verify_scope(record)
        ranked = self._rank(query, records)
        return [
            MemoryEntry(
                content=record.content,
                metadata={
                    **(record.entry_metadata or {}),
                    "memory_id": record.entry_hash,
                },
            )
            for record in ranked[:limit]
        ]

    async def add(self, content: str, metadata: dict[str, Any] | None = None) -> str:
        value = content.strip()
        if not value:
            raise ValueError("memory content must not be empty")
        await self.initialize()
        # Extraction retries are at-least-once. Content identity makes the default
        # SQL store idempotent without collapsing the same sentence across users.
        entry_hash = self._digest(value)
        async with self.database.session() as session, session.begin():
            record = await session.get(
                StrandsMemoryRecord,
                (self.namespace, self._scope_hash, entry_hash),
            )
            if record is None:
                session.add(
                    StrandsMemoryRecord(
                        namespace=self.namespace,
                        scope_hash=self._scope_hash,
                        entry_hash=entry_hash,
                        scope_key=self.scope_key,
                        content=value,
                        entry_metadata=dict(metadata or {}),
                    )
                )
            else:
                self._verify_scope(record)
                record.content = value
                record.entry_metadata = dict(metadata or {})
        return entry_hash

    async def clear(self) -> None:
        """Delete only this Agent/user scope."""

        await self.initialize()
        async with self.database.session() as session, session.begin():
            await session.execute(
                delete(StrandsMemoryRecord).where(
                    StrandsMemoryRecord.namespace == self.namespace,
                    StrandsMemoryRecord.scope_hash == self._scope_hash,
                    StrandsMemoryRecord.scope_key == self.scope_key,
                )
            )

    @classmethod
    async def clear_namespace(cls, database: AsyncDatabase, namespace: str) -> int:
        """Delete all user scopes belonging to one Agent memory namespace."""

        async with database.session() as session, session.begin():
            result = await session.execute(
                delete(StrandsMemoryRecord).where(StrandsMemoryRecord.namespace == namespace)
            )
        return affected_rows(result)

    def get_tools(self) -> list[AgentTool]:
        return []

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _verify_scope(self, record: StrandsMemoryRecord) -> None:
        if record.scope_key != self.scope_key:
            raise RuntimeError("SHA-256 collision detected in Strands memory scope")

    @staticmethod
    def _rank(query: str, records: Sequence[StrandsMemoryRecord]) -> list[StrandsMemoryRecord]:
        phrase = query.strip().casefold()
        if not phrase:
            return list(records)
        tokens = list(dict.fromkeys(re.findall(r"\w+", phrase, flags=re.UNICODE)))

        def score(record: StrandsMemoryRecord) -> tuple[int, int]:
            content = record.content.casefold()
            phrase_score = 10 if phrase in content else 0
            token_score = sum(content.count(token) for token in tokens)
            return phrase_score + token_score, -len(content)

        matching = [record for record in records if score(record)[0] > 0]
        return sorted(matching, key=score, reverse=True)
