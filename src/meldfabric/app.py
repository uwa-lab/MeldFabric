"""High-level AgentKit application entry point."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from meldfabric.agents.runtime import AgentRuntimeManager
from meldfabric.config import (
    ConfigRepository,
    ConfigStore,
    GenerationState,
    LoadedWorkspace,
    ReconciliationReport,
    WorkspaceConfig,
    reconcile_workspace,
)
from meldfabric.config.structured import validate_workspace_structured_data
from meldfabric.config.types import EncryptedSecretWrite
from meldfabric.errors import CodeCatalogConflictError, ConfigurationConflictError
from meldfabric.identity import (
    ClaimedIdentityResolver,
    IdentityResolver,
    load_identity_resolver,
)
from meldfabric.mcp import MCPManager
from meldfabric.models import ModelAdapterRegistry
from meldfabric.observability import ObservabilityManager
from meldfabric.registrations import RegistrationRuntime
from meldfabric.remote import RemoteAgentManager
from meldfabric.resources import CapabilityRegistry
from meldfabric.resources.features import FeatureRegistry
from meldfabric.runtime import RuntimeDriver, RuntimeSnapshot, SnapshotRegistry, driver_for
from meldfabric.sandbox import SandboxManager
from meldfabric.skills import SkillManager
from meldfabric.storage import PersistenceManager, StorageManager
from meldfabric.tools import WorkspaceToolRegistry

logger = logging.getLogger(__name__)


class AgentKit:
    """Configuration-driven MeldFabric workspace.

    Instantiation creates a missing default configuration and publishes the first immutable
    runtime snapshot. Heavy runtimes and optional integrations are loaded only when used.
    """

    def __init__(
        self,
        control_database: str | None = None,
        *,
        state_dir: str | Path = ".meldfabric",
        database_options: dict[str, Any] | None = None,
        admin_key: str | None = None,
        admin_key_env: str = "MELDFABRIC_ADMIN_KEY",
        capability_registry: CapabilityRegistry | None = None,
        identity_resolver: IdentityResolver | None = None,
        identity_resolver_path: str | None = None,
    ) -> None:
        if identity_resolver is not None and identity_resolver_path is not None:
            raise ValueError("provide identity_resolver or identity_resolver_path, not both")
        self.capabilities = capability_registry or CapabilityRegistry()
        self.identity_resolver_path = identity_resolver_path
        self.identity_resolver = (
            identity_resolver
            or (load_identity_resolver(identity_resolver_path) if identity_resolver_path else None)
            or ClaimedIdentityResolver()
        )
        self.model_adapters = ModelAdapterRegistry()
        self.registrations = RegistrationRuntime()
        self.repository = ConfigRepository(
            control_database,
            state_dir=state_dir,
            database_options=database_options,
            admin_key=admin_key,
            admin_key_env=admin_key_env,
        )
        self.tool_registry = WorkspaceToolRegistry()
        self.config_store = ConfigStore(self.repository, capabilities=self.capabilities)
        self.snapshots = SnapshotRegistry()
        self.storage = StorageManager(self.repository.database)
        self.skills = SkillManager(config_dir=self.repository.state_dir)
        self.sandboxes = SandboxManager()
        self.features = FeatureRegistry(
            self.capabilities,
            sandbox_providers=self.sandboxes.provider_names,
            registrations=self.registrations.names,
        )
        self.config_store.feature_validator = self.features.validate_transition
        self.config_store.candidate_processor = self._reconcile_candidate
        self.mcp = MCPManager(secrets_getter=self.storage.secrets_for)
        self.remote_agents = RemoteAgentManager(
            secrets_getter=self.storage.secrets_for,
            mapping_repository_getter=self.storage.control_repository,
        )
        self.observability = ObservabilityManager(
            secrets_getter=self.storage.secrets_for,
        )
        self.persistence = PersistenceManager(
            config_dir=self.repository.state_dir,
        )
        self.agent_runtime = AgentRuntimeManager(self, config_dir=self.repository.state_dir)
        self._async_patch_lock = asyncio.Lock()
        self._last_reload_error: Exception | None = None
        self._runtime_suspended = False
        latest_raw = self.config_store.initialize()
        active_raw = self.repository.load_active()
        self._loaded_raw = latest_raw
        self._loaded, self._latest_reconciliation = self._reconcile_loaded(latest_raw)
        active, self._active_reconciliation = self._reconcile_loaded(active_raw)
        self._log_reconciliation("active", self._active_reconciliation)
        if latest_raw.config.config_generation != active_raw.config.config_generation:
            self._log_reconciliation("latest", self._latest_reconciliation)
        self.config_store.structured_validator = self._validate_structured_config
        self._revealed_admin_key = latest_raw.revealed_admin_key
        self.tool_registry.capture(self._loaded.config.config_generation)
        self.tool_registry.capture(active.config.config_generation)
        self.snapshots.publish(RuntimeSnapshot.compile(active.config))

    def _validate_structured_config(self, config: WorkspaceConfig) -> None:
        validate_workspace_structured_data(
            config,
            registration_get=self.registrations.get,
            sandbox_schemas=self.sandboxes.provider_schemas(),
            model_parameter_schemas=self.model_adapters.parameter_schemas(),
        )

    def _reconcile_candidate(self, config: WorkspaceConfig) -> WorkspaceConfig:
        return reconcile_workspace(config, self.registrations.definitions()).config

    @property
    def config(self) -> WorkspaceConfig:
        """Return the latest saved configuration used by the management plane."""

        return self._loaded.config

    @property
    def raw_config(self) -> WorkspaceConfig:
        """Return the unmodified latest generation as stored in the control database."""

        return self._loaded_raw.config

    @property
    def catalog_digest(self) -> str:
        return self.registrations.digest()

    @property
    def reconciliation_reports(self) -> dict[str, ReconciliationReport]:
        return {
            "active": self._active_reconciliation,
            "latest": self._latest_reconciliation,
        }

    @property
    def runtime_config(self) -> WorkspaceConfig:
        """Return the explicitly applied configuration used by new Agent runs."""

        return self.snapshots.current.config

    @property
    def admin_key(self) -> str | None:
        """Return a newly generated/initialized key only during its one-time reveal."""

        return self._revealed_admin_key

    async def verify_admin_key(self, value: str) -> bool:
        """Verify the administrator credential without blocking the event loop."""

        return await asyncio.to_thread(self.repository.verify_admin_key, value)

    @property
    def last_reload_error(self) -> Exception | None:
        return self._last_reload_error

    @property
    def runtime_suspended(self) -> bool:
        """Whether this concrete runtime instance rejects all non-health traffic."""

        return self._runtime_suspended

    @property
    def accepts_new_runs(self) -> bool:
        return self._last_reload_error is None

    async def asuspend_runtime(self) -> None:
        """Suspend every currently registered instance until each process restarts."""

        repository = self.storage.system
        if repository is None:
            repository = await self.storage.control_repository(self.runtime_config)
        await repository.suspend_runtime_instances(self.runtime_config.workspace.id)
        self._runtime_suspended = True

    async def asynchronize_management_configuration(self) -> GenerationState:
        """Refresh only the control-plane draft when another Pod saved it."""

        state = await asyncio.to_thread(self.repository.generation_state)
        if state.latest_generation != self.config.config_generation:
            loaded = await asyncio.to_thread(self.config_store.load)
            self._publish_latest(loaded)
        return state

    async def areload(self) -> LoadedWorkspace:
        """Synchronize saved and applied generations from the control database."""

        try:
            await self.asynchronize_configuration()
            return self._loaded
        except Exception as exc:
            self._last_reload_error = exc
            raise

    async def asynchronize_configuration(self) -> GenerationState:
        """Reconcile one process with the lightweight database generation pointers."""

        async with self._async_patch_lock:
            for _attempt in range(5):
                state = await asyncio.to_thread(self.repository.generation_state)
                if state.latest_generation != self.config.config_generation:
                    latest = await asyncio.to_thread(self.config_store.load)
                    self._publish_latest(latest)
                if state.active_generation == self.snapshots.current.revision:
                    self._last_reload_error = None
                    return state
                active_config = await asyncio.to_thread(
                    self.repository.load_generation,
                    state.workspace_id,
                    state.active_generation,
                )
                active_raw = LoadedWorkspace(
                    config=active_config,
                    state_dir=self.repository.state_dir,
                    database=self.repository.database,
                )
                active, active_report = self._reconcile_loaded(active_raw)
                await self._prepare_runtime(active.config)
                confirmed = await asyncio.to_thread(self.repository.generation_state)
                if confirmed.active_generation != state.active_generation:
                    continue
                await self._activate_runtime(active)
                self._active_reconciliation = active_report
                self._last_reload_error = None
                return confirmed
            raise ConfigurationConflictError(
                "active configuration kept changing while this instance prepared it",
                details={"local_generation": self.snapshots.current.revision},
            )

    async def restore_runtime_snapshot(
        self,
        revision: int,
        *,
        retain: bool = False,
    ) -> RuntimeSnapshot:
        """Restore a protected historical revision for a resumable interrupted run."""

        try:
            existing = self.snapshots.snapshot(revision)
        except KeyError:
            raw_config = await asyncio.to_thread(
                self.repository.load_generation,
                self.runtime_config.workspace.id,
                revision,
            )
            loaded, report = self._reconcile_loaded(
                LoadedWorkspace(
                    config=raw_config,
                    state_dir=self.repository.state_dir,
                    database=self.repository.database,
                )
            )
            self._log_reconciliation(f"historical:{revision}", report)
            config = loaded.config
            self.tool_registry.capture(revision)
            existing = RuntimeSnapshot.compile(config)
            self.snapshots.restore(existing, retain=retain)
        else:
            if retain:
                self.snapshots.retain(revision)
        return existing

    async def apatch_config(
        self,
        patch: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_catalog_digest: str | None = None,
        secret_writes: tuple[EncryptedSecretWrite, ...] = (),
    ) -> LoadedWorkspace:
        """Persist a new management generation without changing the runtime."""

        async with self._async_patch_lock:
            self._assert_catalog_digest(expected_catalog_digest)
            await self._assert_cluster_catalog_consistency()
            loaded = await asyncio.to_thread(
                self.config_store.patch,
                patch,
                expected_generation=expected_generation,
                base_config=self.config,
                secret_writes=secret_writes,
            )
            self._publish_latest(loaded)
            return self._loaded

    async def aapply_latest_config(
        self,
        *,
        expected_generation: int,
        expected_catalog_digest: str | None = None,
    ) -> LoadedWorkspace:
        """Explicitly publish and atomically activate the latest saved generation."""

        async with self._async_patch_lock:
            self._assert_catalog_digest(expected_catalog_digest)
            await self._assert_cluster_catalog_consistency()
            latest_raw = await asyncio.to_thread(self.config_store.load)
            if latest_raw.config.config_generation != expected_generation:
                raise ConfigurationConflictError(
                    "configuration changed before it was applied",
                    details={
                        "expected_generation": expected_generation,
                        "current_generation": latest_raw.config.config_generation,
                    },
                )
            latest, _report = self._reconcile_loaded(latest_raw)
            await self._prepare_runtime(latest.config)
            activated_raw = await asyncio.to_thread(
                self.repository.activate_latest,
                expected_generation=expected_generation,
            )
            activated, self._active_reconciliation = self._reconcile_loaded(activated_raw)
            self._publish_latest(latest_raw)
            await self._activate_runtime(activated)
            return activated

    async def acreate_config_snapshot(
        self,
        *,
        label: str | None = None,
        note: str | None = None,
        expected_generation: int | None = None,
    ):
        return await asyncio.to_thread(
            self.repository.create_snapshot,
            label=label,
            note=note,
            expected_generation=expected_generation,
        )

    async def arestore_config_snapshot(
        self,
        version: int,
        *,
        expected_generation: int,
        expected_catalog_digest: str | None = None,
    ) -> LoadedWorkspace:
        async with self._async_patch_lock:
            self._assert_catalog_digest(expected_catalog_digest)
            await self._assert_cluster_catalog_consistency()
            loaded = await asyncio.to_thread(
                self.repository.restore_snapshot,
                version,
                expected_generation=expected_generation,
                validator=self._reconcile_candidate,
            )
            effective, _report = self._reconcile_loaded(loaded)
            self.capabilities.validate_workspace(effective.config, allow_unavailable=True)
            self._publish_latest(loaded)
            return effective

    async def atouch_config(self, *, reason: str) -> LoadedWorkspace:
        """Create a generation after a configuration-bound secret-only change."""

        async with self._async_patch_lock:
            await self._assert_cluster_catalog_consistency()
            current = self.config
            candidate = current.model_copy(
                update={"config_generation": current.config_generation + 1}
            )
            loaded = await asyncio.to_thread(
                self.repository.save,
                candidate,
                expected_generation=current.config_generation,
                reason=reason,
            )
            self._publish_latest(loaded)
            return self._loaded

    def _publish_latest(self, loaded: LoadedWorkspace) -> None:
        if (
            hasattr(self, "_loaded_raw")
            and loaded.config.config_generation < self._loaded_raw.config.config_generation
        ):
            return
        effective, report = self._reconcile_loaded(loaded)
        self.tool_registry.capture(effective.config.config_generation)
        self._loaded_raw = loaded
        self._loaded = effective
        self._latest_reconciliation = report

    def _reconcile_loaded(
        self, loaded: LoadedWorkspace
    ) -> tuple[LoadedWorkspace, ReconciliationReport]:
        result = reconcile_workspace(
            loaded.config,
            self.registrations.definitions(),
        )
        self._validate_structured_config(result.config)
        return (
            LoadedWorkspace(
                config=result.config,
                state_dir=loaded.state_dir,
                database=loaded.database,
                revealed_admin_key=loaded.revealed_admin_key,
                admin_key_prefix=loaded.admin_key_prefix,
            ),
            result.report,
        )

    def _assert_catalog_digest(self, expected: str | None) -> None:
        if expected is None:
            return
        current = self.catalog_digest
        if expected != current:
            raise CodeCatalogConflictError(
                "code registration catalog changed before this operation completed",
                details={
                    "expected_catalog_digest": expected,
                    "current_catalog_digest": current,
                },
            )

    @staticmethod
    def _log_reconciliation(label: str, report: ReconciliationReport) -> None:
        if not report.issues:
            return
        logger.warning(
            "Reconciled %s configuration generation %s against the code catalog: "
            "%s issue(s), %s critical",
            label,
            report.config_generation,
            len(report.issues),
            report.critical_count,
        )

    async def _assert_cluster_catalog_consistency(self) -> None:
        """Reject shared configuration writes while ready Pods run different code."""

        repository = self.storage.system
        if repository is None:
            return
        status = await repository.runtime_coordination_status()
        ready = [item for item in status["instances"] if item["status"] == "ready"]
        inconsistent = [item for item in ready if item.get("catalog_digest") != self.catalog_digest]
        if inconsistent:
            raise CodeCatalogConflictError(
                "ready runtime instances do not share the same code registration catalog",
                details={
                    "current_catalog_digest": self.catalog_digest,
                    "instances": [
                        {
                            "id": item["id"],
                            "hostname": item["hostname"],
                            "catalog_digest": item.get("catalog_digest"),
                        }
                        for item in inconsistent
                    ],
                },
            )

    async def _prepare_runtime(self, config: WorkspaceConfig) -> None:
        await self.storage.initialize_revision(config, make_current=False)
        await self.persistence.initialize_revision(config, make_current=False)

    async def _activate_runtime(self, loaded: LoadedWorkspace) -> None:
        revision = loaded.config.config_generation
        if revision != self.snapshots.current.revision:
            if revision < self.snapshots.current.revision:
                raise RuntimeError("runtime generation cannot move backwards")
            self.tool_registry.capture(revision)
            self.snapshots.publish(RuntimeSnapshot.compile(loaded.config))
        self.agent_runtime.prune()
        await self.mcp.prune(set(self.snapshots.active_revisions()))
        self.remote_agents.prune(set(self.snapshots.active_revisions()))
        self.tool_registry.prune(set(self.snapshots.active_revisions()))
        self.observability.prune(set(self.snapshots.active_revisions()))
        self.sandboxes.prune(set(self.snapshots.active_revisions()))
        self.storage.activate(revision)
        self.persistence.activate(revision)
        active = set(self.snapshots.active_revisions())
        await self.storage.prune(active)
        await self.persistence.prune(active)
        self._last_reload_error = None

    def create_app(self):
        """Create the combined management and business ASGI application."""

        from meldfabric.management import create_management_app

        return create_management_app(self)

    def runtime_driver(self, name: str | None = None) -> RuntimeDriver:
        return driver_for(self, name)

    def tool_catalog(self) -> list[dict[str, Any]]:
        """Merge imported code definitions with persisted administrative overrides."""

        _, definitions = self.tool_registry.snapshot(self.config.config_generation, refresh=True)
        overrides = {item.id: item for item in self.config.resources.tools}
        result: list[dict[str, Any]] = []
        for tool_id in sorted(definitions.keys() | overrides.keys()):
            definition = definitions.get(tool_id)
            override = overrides.get(tool_id)
            if definition is None and override is None:
                continue
            code_description = definition.description if definition else ""
            description_override = (
                override.description if override and override.description else None
            )
            parameter_overrides = dict(override.parameter_descriptions) if override else {}
            input_schema = definition.input_schema if definition else None
            properties = input_schema.get("properties", {}) if input_schema else {}
            required = set(input_schema.get("required", ())) if input_schema else set()
            parameters = []
            for parameter_name, parameter_schema in properties.items():
                code_parameter_description = (
                    parameter_schema.get("description", "")
                    if isinstance(parameter_schema, dict)
                    else ""
                )
                parameters.append(
                    {
                        "name": parameter_name,
                        "schema": parameter_schema,
                        "required": parameter_name in required,
                        "description": parameter_overrides.get(
                            parameter_name, code_parameter_description
                        ),
                        "description_default": code_parameter_description,
                        "description_overridden": parameter_name in parameter_overrides,
                    }
                )
            if override is not None:
                tool_name = override.name
                display_name = override.display_name
                python_path = override.python_path
            else:
                if definition is None:  # Guarded above; keeps the data boundary explicit.
                    continue
                tool_name = definition.name
                display_name = definition.display_name
                python_path = definition.python_path
            result.append(
                {
                    "id": tool_id,
                    "registration_id": tool_id,
                    "registration_digest": (override.registration_digest if override else None),
                    "name": tool_name,
                    "display_name": display_name,
                    "description": (
                        description_override
                        if description_override is not None
                        else code_description
                    ),
                    "description_default": code_description,
                    "description_overridden": description_override is not None,
                    "python_path": definition.python_path if definition else python_path,
                    "enabled": override.enabled if override else True,
                    "available": definition is not None,
                    "requires_sandbox": (
                        (override.requires_sandbox if override else False)
                        or (definition.requires_sandbox if definition else False)
                    ),
                    "parameter_descriptions": parameter_overrides,
                    "parameters": parameters,
                    "input_schema": input_schema,
                    "return_schema": definition.return_schema if definition else None,
                    "managed_by": "code",
                    "bindings": self._tool_bindings(tool_id),
                }
            )
        return result

    def _tool_bindings(self, tool_id: str) -> list[dict[str, str]]:
        bindings: list[dict[str, str]] = []
        for agent in self.config.agents:
            for binding in agent.tool_bindings:
                if binding.tool_ref == tool_id:
                    bindings.append({"agent_id": agent.id, "binding_id": binding.id})
        return bindings

    def run(self, *, host: str | None = None, port: int | None = None) -> None:
        self.runtime_driver().run(host=host, port=port)
