"""Revision-aware workspace storage lifecycle and database resource reuse."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from meldfabric.config.database import ControlDatabaseSpec
from meldfabric.config.models import DatabaseConnection, WorkspaceConfig
from meldfabric.config.schema import ControlWorkspace
from meldfabric.errors import ConfigurationError
from meldfabric.security.crypto import SecretCipher, SecretDecryptionError
from meldfabric.storage.database import AsyncDatabase
from meldfabric.storage.playground import PlaygroundRepository
from meldfabric.storage.secrets import GenerationSecretRepository, SecretRepository
from meldfabric.storage.system import SystemRepository


@dataclass(slots=True)
class StorageRevision:
    revision: int
    databases: dict[str, AsyncDatabase] = field(default_factory=dict)
    system_repositories: dict[str, SystemRepository] = field(default_factory=dict)
    system: SystemRepository | None = None
    secrets: GenerationSecretRepository | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)


class StorageManager:
    """Keeps old database handles alive while their Runtime Snapshot is pinned."""

    def __init__(self, control_database: ControlDatabaseSpec) -> None:
        self.control_database = control_database
        self.config_dir = control_database.state_dir
        self._revisions: dict[int, StorageRevision] = {}
        self._current_revision: int | None = None
        self._lock = asyncio.Lock()
        self._control = AsyncDatabase(control_database)
        self._control_system: SystemRepository | None = None
        self._playground: PlaygroundRepository | None = None
        self._control_secrets: SecretRepository | None = None
        self._workspace_id: str | None = None

    @property
    def current(self) -> StorageRevision | None:
        if self._current_revision is None:
            return None
        return self._revisions.get(self._current_revision)

    @property
    def system(self) -> SystemRepository | None:
        return self._control_system

    @property
    def secrets(self) -> SecretRepository | None:
        return self._control_secrets

    @property
    def playground(self) -> PlaygroundRepository | None:
        return self._playground

    @property
    def issues(self) -> list[dict[str, Any]]:
        current = self.current
        return list(current.issues) if current is not None else []

    async def initialize(self, config: WorkspaceConfig) -> None:
        await self.initialize_revision(config, make_current=True)

    def validate_secret_storage(self, config: WorkspaceConfig) -> SecretCipher:
        """Check storage selection and resolve the workspace encryption key."""
        return SecretCipher.from_environment(
            "MELDFABRIC_MASTER_KEY",
            default_path=self.config_dir / "keys" / f"{config.workspace.id}.key",
        )

    async def require_secrets(self, config: WorkspaceConfig) -> SecretRepository:
        """Return a ready repository or report the actual missing prerequisite.

        Retry a previously unavailable repository after its key file or environment
        is repaired, without requiring an unrelated configuration revision.
        Never replace the cipher of an already-initialized repository.
        """
        await self.initialize_revision(config, make_current=False)
        if self._control_secrets is None:
            raise ConfigurationError("control secret repository is not initialized")
        return self._control_secrets

    async def initialize_revision(
        self,
        config: WorkspaceConfig,
        *,
        make_current: bool = True,
    ) -> StorageRevision:
        async with self._lock:
            if self._workspace_id is not None and self._workspace_id != config.workspace.id:
                raise ConfigurationError("storage manager cannot span multiple workspaces")
            self._workspace_id = config.workspace.id
            existing = self._revisions.get(config.config_generation)
            if existing is not None:
                if make_current:
                    self._current_revision = config.config_generation
                return existing

            bundle = StorageRevision(revision=config.config_generation)
            self._revisions[config.config_generation] = bundle
            if self._control_system is None:
                self._control_system = SystemRepository(self._control)
                await self._control_system.initialize()
                self._playground = PlaygroundRepository(self._control)
            if self._control_secrets is None:
                cipher = await asyncio.to_thread(self.validate_secret_storage, config)
                await self._validate_control_cipher(config, cipher)
                self._control_secrets = SecretRepository(self._control, cipher)
                await self._control_secrets.initialize()
            bundle.system = self._control_system
            bundle.secrets = GenerationSecretRepository(
                self._control_secrets,
                workspace_id=config.workspace.id,
                generation=config.config_generation,
            )
            if make_current:
                self._current_revision = config.config_generation
            return bundle

    async def _validate_control_cipher(
        self,
        config: WorkspaceConfig,
        cipher: SecretCipher,
    ) -> None:
        async with self._control.session() as session, session.begin():
            workspace = await session.get(ControlWorkspace, config.workspace.id)
            if workspace is None:
                raise ConfigurationError("control workspace record is missing")
            if workspace.encryption_key_fingerprint is None:
                workspace.encryption_key_fingerprint = cipher.fingerprint
                return
            if workspace.encryption_key_fingerprint != cipher.fingerprint:
                raise SecretDecryptionError(
                    "the configured encryption key does not belong to this workspace",
                    details={
                        "workspace_id": config.workspace.id,
                        "action": "restore_the_original_workspace_key_file",
                    },
                )

    def for_revision(self, revision: int) -> StorageRevision:
        try:
            return self._revisions[revision]
        except KeyError as exc:
            raise ConfigurationError(
                f"storage resources for configuration revision {revision} are not initialized"
            ) from exc

    def activate(self, revision: int) -> None:
        self.for_revision(revision)
        self._current_revision = revision

    def secrets_for(self, revision: int) -> GenerationSecretRepository | None:
        bundle = self._revisions.get(revision)
        if bundle is not None and bundle.secrets is not None:
            return bundle.secrets
        if self._control_secrets is None or self._workspace_id is None:
            return None
        return GenerationSecretRepository(
            self._control_secrets,
            workspace_id=self._workspace_id,
            generation=revision,
        )

    def system_for(self, revision: int) -> SystemRepository | None:
        return self.for_revision(revision).system

    def ready_system_for(self, revision: int) -> SystemRepository | None:
        bundle = self._revisions.get(revision)
        return bundle.system if bundle is not None else None

    def all_system_repositories(self) -> tuple[SystemRepository, ...]:
        unique: dict[str, SystemRepository] = {}
        if self._control_system is not None:
            unique[self._control_system.database.url] = self._control_system
        for bundle in self._revisions.values():
            for repository in bundle.system_repositories.values():
                unique[repository.database.url] = repository
        return tuple(unique.values())

    def _database(
        self,
        bundle: StorageRevision,
        databases: dict[str, DatabaseConnection],
        reference: str,
    ) -> AsyncDatabase:
        if reference not in databases:
            raise ConfigurationError(f"database resource {reference!r} does not exist")
        if reference not in bundle.databases:
            bundle.databases[reference] = AsyncDatabase(
                databases[reference],
                config_dir=self.config_dir,
            )
        return bundle.databases[reference]

    async def _system_repository(
        self,
        bundle: StorageRevision,
        databases: dict[str, DatabaseConnection],
        database_ref: str,
    ) -> SystemRepository:
        existing = bundle.system_repositories.get(database_ref)
        if existing is not None:
            return existing
        database = await asyncio.to_thread(self._database, bundle, databases, database_ref)
        repository = SystemRepository(database)
        await repository.initialize()
        bundle.system_repositories[database_ref] = repository
        return repository

    async def system_repository(
        self,
        config: WorkspaceConfig,
        database_ref: str,
    ) -> SystemRepository:
        bundle = self._revisions.get(config.config_generation)
        if bundle is None:
            bundle = await self.initialize_revision(config, make_current=False)
        databases = {item.id: item for item in config.resources.databases}
        return await self._system_repository(bundle, databases, database_ref)

    async def control_repository(self, config: WorkspaceConfig) -> SystemRepository:
        await self.initialize_revision(config, make_current=False)
        if self._control_system is None:
            raise ConfigurationError("control system repository is not initialized")
        return self._control_system

    async def prune(self, active_revisions: set[int]) -> None:
        async with self._lock:
            retired = [
                revision
                for revision in self._revisions
                if revision not in active_revisions and revision != self._current_revision
            ]
            for revision in retired:
                bundle = self._revisions.pop(revision)
                await self._close_bundle(bundle)

    async def close(self) -> None:
        async with self._lock:
            bundles = list(self._revisions.values())
            self._revisions.clear()
            self._current_revision = None
            self._workspace_id = None
            for bundle in bundles:
                await self._close_bundle(bundle)
            self._control_system = None
            self._playground = None
            self._control_secrets = None
            await self._control.close()

    async def test_database(self, connection: DatabaseConnection) -> dict[str, Any]:
        """Open an isolated connection and execute a side-effect-free health query."""

        from sqlalchemy import text

        started = time.perf_counter()
        database: AsyncDatabase | None = None
        try:
            database = await asyncio.to_thread(
                AsyncDatabase, connection, config_dir=self.config_dir
            )
            async with database.engine.connect() as sql_connection:
                await sql_connection.execute(text("SELECT 1"))
        except Exception as exc:
            return {
                "reachable": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if database is not None:
                await database.close()
        return {
            "reachable": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": None,
        }

    @staticmethod
    async def _close_bundle(bundle: StorageRevision) -> None:
        databases = list(bundle.databases.values())
        bundle.databases.clear()
        bundle.system_repositories.clear()
        bundle.system = None
        bundle.secrets = None
        for database in databases:
            await database.close()

    def status(self) -> dict[str, Any]:
        current = self.current
        return {
            "revision": self._current_revision,
            "active_revisions": sorted(self._revisions),
            "control_database": "ready" if self._control_system is not None else "not_ready",
            "system_schema_version": (
                self._control_system.schema_version if self._control_system is not None else None
            ),
            "secret_store": "ready" if self._control_secrets is not None else "not_ready",
            "issues": list(current.issues) if current is not None else [],
        }
