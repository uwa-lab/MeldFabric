"""Revision-owned Strands session and memory database adapters."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import AsyncFileLock

from meldfabric.config.models import (
    ContextOffloaderPluginResource,
    DatabaseConnection,
    WorkspaceConfig,
)
from meldfabric.domain import DatabaseEngine
from meldfabric.errors import AgentKitError, AgentNotReadyError, ConfigurationError
from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.strands import SQLStorage


@dataclass(slots=True)
class PersistenceRevision:
    revision: int
    stack: AsyncExitStack = field(default_factory=AsyncExitStack)
    strands_storages: dict[str, SQLStorage] = field(default_factory=dict)
    strands_databases: dict[str, AsyncDatabase] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


class PersistenceManager:
    """Build adapters per configuration generation and retire them after use."""

    def __init__(self, *, config_dir: Path) -> None:
        self.config_dir = config_dir
        self._revisions: dict[int, PersistenceRevision] = {}
        self._current_revision: int | None = None
        self._lock = asyncio.Lock()

    @property
    def current(self) -> PersistenceRevision | None:
        if self._current_revision is None:
            return None
        return self._revisions.get(self._current_revision)

    async def initialize(self, config: WorkspaceConfig) -> None:
        await self.initialize_revision(config, make_current=True)

    async def initialize_revision(
        self,
        config: WorkspaceConfig,
        *,
        make_current: bool = True,
    ) -> PersistenceRevision:
        async with self._lock:
            existing = self._revisions.get(config.config_generation)
            if existing is not None:
                if make_current:
                    self._current_revision = config.config_generation
                return existing

            bundle = PersistenceRevision(revision=config.config_generation)
            self._revisions[config.config_generation] = bundle
            databases = {item.id: item for item in config.resources.databases}
            usages: dict[str, set[str]] = {}
            for agent in config.agents:
                if agent.session_manager is not None:
                    usages.setdefault(agent.session_manager.database_ref, set()).add(
                        "strands_session"
                    )
                if agent.memory_manager is not None:
                    usages.setdefault(agent.memory_manager.database_ref, set()).add(
                        "strands_memory"
                    )
            for orchestration in config.orchestrations:
                session = getattr(orchestration, "session_manager", None)
                if session is not None:
                    usages.setdefault(session.database_ref, set()).add(
                        "strands_orchestration_session"
                    )
            for plugin in config.resources.plugins:
                if isinstance(plugin, ContextOffloaderPluginResource):
                    usages.setdefault(plugin.database_ref, set()).add("strands_plugin_storage")

            for reference, reference_usages in usages.items():
                try:
                    await self._open_strands_storage(
                        bundle,
                        self._require_database(databases, reference),
                    )
                except Exception as exc:
                    bundle.issues.append(
                        {
                            "code": (
                                exc.code
                                if isinstance(exc, AgentKitError)
                                else "STRANDS_STORAGE_UNAVAILABLE"
                            ),
                            "message": (
                                exc.message
                                if isinstance(exc, AgentKitError)
                                else f"Strands SQL initialization failed ({type(exc).__name__})"
                            ),
                            "database_ref": reference,
                            "usages": sorted(reference_usages),
                        }
                    )
            if make_current:
                self._current_revision = config.config_generation
            return bundle

    def for_revision(self, revision: int) -> PersistenceRevision:
        try:
            return self._revisions[revision]
        except KeyError as exc:
            raise ConfigurationError(
                f"persistence adapters for configuration revision {revision} are not initialized"
            ) from exc

    def activate(self, revision: int) -> None:
        self.for_revision(revision)
        self._current_revision = revision

    async def strands_storage_for_database(
        self,
        config: WorkspaceConfig,
        database_ref: str,
    ) -> SQLStorage:
        bundle = self.for_revision(config.config_generation)
        existing = bundle.strands_storages.get(database_ref)
        if existing is not None:
            return existing
        databases = {item.id: item for item in config.resources.databases}
        return await self._open_strands_storage(
            bundle,
            self._require_database(databases, database_ref),
        )

    async def strands_database_for_database(
        self,
        config: WorkspaceConfig,
        database_ref: str,
    ) -> AsyncDatabase:
        bundle = self.for_revision(config.config_generation)
        if database_ref not in bundle.strands_databases:
            await self.strands_storage_for_database(config, database_ref)
        return bundle.strands_databases[database_ref]

    @staticmethod
    def _require_database(
        databases: dict[str, DatabaseConnection],
        reference: str,
    ) -> DatabaseConnection:
        try:
            return databases[reference]
        except KeyError as exc:
            raise AgentNotReadyError(
                f"persistence database resource {reference!r} does not exist"
            ) from exc

    async def _open_strands_storage(
        self,
        bundle: PersistenceRevision,
        database: DatabaseConnection,
    ) -> SQLStorage:
        existing = bundle.strands_storages.get(database.id)
        if existing is not None:
            return existing
        runtime_database = await asyncio.to_thread(
            AsyncDatabase,
            database,
            config_dir=self.config_dir,
            bridge_safe=True,
        )
        storage = SQLStorage(runtime_database, namespace="meldfabric")
        try:
            await self._setup_schema(storage, database, runtime_database)
        except Exception:
            await runtime_database.close()
            raise
        bundle.strands_storages[database.id] = storage
        bundle.strands_databases[database.id] = runtime_database
        bundle.stack.push_async_callback(runtime_database.close)
        return storage

    async def _setup_schema(
        self,
        storage: SQLStorage,
        database: DatabaseConnection,
        runtime_database: AsyncDatabase,
    ) -> None:
        """Serialize first-use DDL across processes and Pods."""

        if database.engine is DatabaseEngine.SQLITE:
            path = runtime_database.url.removeprefix("sqlite+aiosqlite:///")
            if path == ":memory:":
                await storage.initialize()
                return
            async with AsyncFileLock(path + ".strands-setup.lock", timeout=30):
                await storage.initialize()
            return

        from sqlalchemy import text

        identity = f"meldfabric:strands:{database.url}"
        lock_database = await asyncio.to_thread(
            AsyncDatabase,
            database,
            config_dir=self.config_dir,
        )
        try:
            async with lock_database.engine.connect() as connection:
                if database.engine is DatabaseEngine.POSTGRESQL:
                    lock_id = int(hashlib.sha256(identity.encode()).hexdigest()[:15], 16)
                    await connection.execute(
                        text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id}
                    )
                    try:
                        await storage.initialize()
                    finally:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_id)"),
                            {"lock_id": lock_id},
                        )
                    return
                if database.engine is DatabaseEngine.MYSQL:
                    lock_name = "meldfabric:" + hashlib.sha256(identity.encode()).hexdigest()[:48]
                    acquired = await connection.scalar(
                        text("SELECT GET_LOCK(:name, 30)"), {"name": lock_name}
                    )
                    if acquired != 1:
                        raise TimeoutError("timed out acquiring the Strands schema lock")
                    try:
                        await storage.initialize()
                    finally:
                        await connection.execute(
                            text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}
                        )
                    return
                await storage.initialize()
        finally:
            await lock_database.close()

    async def prune(self, active_revisions: set[int]) -> None:
        async with self._lock:
            retired = [
                revision
                for revision in self._revisions
                if revision not in active_revisions and revision != self._current_revision
            ]
            for revision in retired:
                bundle = self._revisions.pop(revision)
                await bundle.stack.aclose()

    async def close(self) -> None:
        async with self._lock:
            bundles = list(self._revisions.values())
            self._revisions.clear()
            self._current_revision = None
            for bundle in bundles:
                await bundle.stack.aclose()

    def status(self) -> dict[str, Any]:
        current = self.current
        return {
            "revision": self._current_revision,
            "active_revisions": sorted(self._revisions),
            "strands_storages": (sorted(current.strands_storages) if current is not None else []),
            "strands_memory_databases": (
                sorted(current.strands_databases) if current is not None else []
            ),
            "issues": list(current.issues) if current is not None else [],
        }
