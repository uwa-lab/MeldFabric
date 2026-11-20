"""Dynamic capability registry used by configuration and the management UI."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from enum import StrEnum
from functools import cache

from meldfabric.config.models import (
    ContextOffloaderPluginResource,
    DatabaseConnection,
    WorkspaceConfig,
)
from meldfabric.domain.enums import DatabaseEngine
from meldfabric.errors import ResourceCapabilityError, ResourceNotFoundError


class Capability(StrEnum):
    RELATIONAL = "relational"
    SESSION_STORAGE = "session_storage"
    MEMORY_STORAGE = "memory_storage"
    VECTOR_SEARCH = "vector_search"


class CapabilityRegistry:
    """Maps installed adapters to the capabilities they provide.

    The registry, rather than the frontend, owns compatibility decisions. Third-party
    adapters can extend the mapping at application startup.
    """

    def __init__(
        self,
        *,
        module_available: Callable[[str], bool] | None = None,
    ) -> None:
        self._module_available = module_available or self._find_module
        self._database_capabilities: dict[DatabaseEngine, set[Capability]] = {
            DatabaseEngine.SQLITE: set(),
            DatabaseEngine.POSTGRESQL: set(),
            DatabaseEngine.MYSQL: set(),
            DatabaseEngine.CUSTOM: set(),
        }

    @staticmethod
    @cache
    def _find_module(name: str) -> bool:
        try:
            importlib.import_module(name)
            return True
        except Exception:
            return False

    def module_available(self, name: str) -> bool:
        return self._module_available(name)

    def registered_database_engines(self) -> list[str]:
        return [engine.value for engine, values in self._database_capabilities.items() if values]

    @staticmethod
    def _common_capabilities() -> set[Capability]:
        return {
            Capability.RELATIONAL,
            Capability.SESSION_STORAGE,
            Capability.MEMORY_STORAGE,
        }

    def _builtin_database_capabilities(
        self,
        database: DatabaseConnection,
    ) -> set[Capability]:
        if database.engine is DatabaseEngine.SQLITE:
            return self._common_capabilities()
        if database.engine is DatabaseEngine.POSTGRESQL:
            capabilities: set[Capability] = set()
            sqlalchemy_driver_ready = (
                "+asyncpg" in database.url and self._module_available("asyncpg")
            ) or ("+psycopg" in database.url and self._module_available("psycopg"))
            if sqlalchemy_driver_ready:
                capabilities.update(self._common_capabilities())
            return capabilities
        if database.engine is DatabaseEngine.MYSQL:
            if "+asyncmy" in database.url and self._module_available("asyncmy"):
                return self._common_capabilities()
            return set()
        return set()

    def register_database_adapter(
        self,
        engine: DatabaseEngine | str,
        capabilities: set[Capability | str],
    ) -> None:
        engine_value = DatabaseEngine(engine)
        resolved = {Capability(item) for item in capabilities}
        self._database_capabilities.setdefault(engine_value, set()).update(resolved)

    def database_capabilities(self, database: DatabaseConnection) -> frozenset[Capability]:
        return frozenset(
            self._builtin_database_capabilities(database)
            | self._database_capabilities.get(database.engine, set())
        )

    def compatible_databases(
        self,
        config: WorkspaceConfig,
        required: Capability | str,
        *,
        include_disabled: bool = False,
    ) -> list[DatabaseConnection]:
        required_capability = Capability(required)
        return [
            item
            for item in config.resources.databases
            if (include_disabled or item.enabled)
            and required_capability in self.database_capabilities(item)
        ]

    @staticmethod
    def _database_uses(config: WorkspaceConfig):
        for agent in config.agents:
            if not agent.enabled:
                continue
            for field, capability in (
                ("session_manager", Capability.SESSION_STORAGE),
                ("memory_manager", Capability.MEMORY_STORAGE),
            ):
                binding = getattr(agent, field)
                if binding is not None:
                    yield (
                        f"agent {agent.id}.{field}.database_ref",
                        binding.database_ref,
                        capability,
                        True,
                    )
        for orchestration in config.orchestrations:
            session = getattr(orchestration, "session_manager", None)
            if orchestration.enabled and session is not None:
                yield (
                    f"orchestration {orchestration.id}.session_manager.database_ref",
                    session.database_ref,
                    Capability.SESSION_STORAGE,
                    True,
                )
        for plugin in config.resources.plugins:
            if plugin.enabled and isinstance(plugin, ContextOffloaderPluginResource):
                yield (
                    f"plugin {plugin.id}.database_ref",
                    plugin.database_ref,
                    Capability.MEMORY_STORAGE,
                    True,
                )

    def validate_workspace(
        self,
        config: WorkspaceConfig,
        *,
        previous: WorkspaceConfig | None = None,
        allow_unavailable: bool = False,
    ) -> None:
        databases = {item.id: item for item in config.resources.databases}
        old_databases = {item.id: item for item in previous.resources.databases} if previous else {}
        old_uses = (
            {path: (ref, cap) for path, ref, cap, _ in self._database_uses(previous)}
            if previous
            else {}
        )
        for path, reference, capability, required in self._database_uses(config):
            unchanged = old_uses.get(path) == (reference, capability) and databases.get(
                reference
            ) == old_databases.get(reference)
            self._validate_database_ref(
                databases,
                reference,
                capability,
                path,
                required=required,
                check_capability=not (allow_unavailable or unchanged),
            )

    def _validate_database_ref(
        self,
        databases: dict[str, DatabaseConnection],
        reference: str | None,
        capability: Capability,
        path: str,
        *,
        required: bool = False,
        check_capability: bool = True,
    ) -> None:
        if reference is None:
            if required:
                raise ResourceNotFoundError(f"{path} is required")
            return
        database = databases.get(reference)
        if database is None:
            raise ResourceNotFoundError(
                f"{path} references unknown database {reference!r}",
                details={"path": path, "database_ref": reference},
            )
        if not database.enabled:
            raise ResourceCapabilityError(
                f"{path} references disabled database {reference!r}",
                details={"path": path, "database_ref": reference},
            )
        capabilities = self.database_capabilities(database)
        if check_capability and capability not in capabilities:
            raise ResourceCapabilityError(
                f"database {reference!r} does not provide capability {capability.value!r}",
                details={
                    "path": path,
                    "database_ref": reference,
                    "required_capability": capability.value,
                    "available_capabilities": sorted(item.value for item in capabilities),
                },
            )
