"""Installed capabilities shared by the admin UI, mutation checks and runtime.

Availability describes local adapters, never remote service health. Existing unavailable
configuration remains readable; mutations may preserve, disable or remove it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from meldfabric.config.models import (
    AgentDefinition,
    RegisteredInterventionResource,
    RegisteredPluginResource,
    WorkspaceConfig,
)
from meldfabric.errors import AgentNotReadyError, ResourceCapabilityError
from meldfabric.resources.capabilities import CapabilityRegistry


@dataclass(frozen=True)
class FeatureUse:
    key: str
    feature: str
    value: Any
    resource_type: str
    resource_id: str
    label: str
    agent_id: str | None = None


class FeatureRegistry:
    def __init__(
        self,
        capabilities: CapabilityRegistry,
        *,
        sandbox_providers: Callable[[], tuple[str, ...]],
        registrations: Callable[[], dict[str, list[str]]],
    ) -> None:
        self.capabilities = capabilities
        self.sandbox_providers = sandbox_providers
        self.registrations = registrations

    def catalog(self) -> dict[str, Any]:
        available = self.capabilities.module_available
        features: dict[str, dict[str, Any]] = {}

        def add(name, label, ready, extra=None):
            features[name] = {
                "label": label,
                "available": bool(ready),
                "extra": extra,
                "reason": None
                if ready
                else f"{label} adapter is not installed or cannot be loaded",
            }

        add("mcp", "MCP", available("strands.tools.mcp"))
        add("langfuse", "Langfuse", available("langfuse"), "langfuse")
        add("jwt", "JWT", available("jwt"))
        add("sandbox", "沙箱", bool(self.sandbox_providers()), "sandbox-docker")
        registrations = self.registrations()
        add("plugins", "Strands 插件", True)
        add("hooks", "Strands Hooks", True)
        add("interventions", "Strands Interventions", True)
        # Driver-level choices: PostgreSQL's two SQLAlchemy drivers need separate checks.
        drivers = {
            "sqlite": ["sqlite+aiosqlite"],
            "postgresql": [
                driver
                for driver, module in (
                    ("postgresql+asyncpg", "asyncpg"),
                    ("postgresql+psycopg", "psycopg"),
                )
                if available(module)
            ],
            "mysql": ["mysql+asyncmy"] if available("asyncmy") else [],
        }
        registered_engines = self.capabilities.registered_database_engines()
        for engine, extra in (
            ("sqlite", None),
            ("postgresql", None),
            ("mysql", None),
            ("custom", None),
        ):
            add(
                "database:" + engine,
                engine,
                bool(drivers.get(engine)) or engine in registered_engines,
                extra,
            )
        for provider in self.sandbox_providers():
            add("sandbox:" + provider, provider, True)
        for kind in (
            "plugin",
            "hook",
            "intervention",
            "graph_condition",
        ):
            for name in registrations[kind]:
                add(kind + ":" + name, name, True)
        return {
            "features": features,
            "database_engines": sorted(
                {key for key, values in drivers.items() if values} | set(registered_engines)
            ),
            "database_drivers": drivers,
            "sandbox_providers": list(self.sandbox_providers()),
            "runtime_drivers": ["embedded_fastapi"],
            "resource_pools": {
                "mcp_servers": features["mcp"]["available"],
                "sandbox_profiles": features["sandbox"]["available"],
                "observability_providers": features["langfuse"]["available"],
                "plugins": True,
                "hooks": True,
                "interventions": True,
            },
        }

    @staticmethod
    def _status(catalog: dict[str, Any], feature: str) -> dict[str, Any]:
        status = catalog["features"].get(feature)
        if status is not None:
            return status
        extra = "sandbox-docker" if feature == "sandbox:docker" else None
        return {
            "available": False,
            "label": feature,
            "extra": extra,
            "reason": f"Adapter {feature!r} is not registered",
        }

    def _uses(self, config: WorkspaceConfig) -> list[FeatureUse]:
        uses: list[FeatureUse] = []
        resources = config.resources
        sandbox = {item.id: item for item in resources.sandbox_profiles}
        for pool, feature_for in (
            ("mcp_servers", lambda _: "mcp"),
            ("observability_providers", lambda _: "langfuse"),
            ("sandbox_profiles", lambda item: "sandbox:" + item.provider),
        ):
            for item in getattr(resources, pool):
                uses.append(
                    FeatureUse(
                        f"{pool}:{item.id}",
                        feature_for(item),
                        item.model_dump(mode="json"),
                        pool,
                        item.id,
                        item.name,
                    )
                )
        for db in resources.databases:
            if not self.capabilities.database_capabilities(db):
                uses.append(
                    FeatureUse(
                        f"databases:{db.id}",
                        "database:" + db.engine.value,
                        db.model_dump(mode="json"),
                        "databases",
                        db.id,
                        db.name,
                    )
                )
        for item in resources.plugins:
            if isinstance(item, RegisteredPluginResource):
                uses.append(
                    FeatureUse(
                        f"plugins:{item.id}",
                        "plugin:" + item.registration_id,
                        item.model_dump(mode="json"),
                        "plugins",
                        item.id,
                        item.name,
                    )
                )
        for item in resources.hooks:
            uses.append(
                FeatureUse(
                    f"hooks:{item.id}",
                    "hook:" + item.registration_id,
                    item.model_dump(mode="json"),
                    "hooks",
                    item.id,
                    item.name,
                )
            )
        for item in resources.interventions:
            if isinstance(item, RegisteredInterventionResource):
                uses.append(
                    FeatureUse(
                        f"interventions:{item.id}",
                        "intervention:" + item.registration_id,
                        item.model_dump(mode="json"),
                        "interventions",
                        item.id,
                        item.name,
                    )
                )

        def add_agent(
            agent: AgentDefinition,
            owner: str,
            key: str,
            feature: str,
            value: Any,
        ):
            uses.append(
                FeatureUse(
                    f"agent:{owner}:{key}",
                    feature,
                    value,
                    "agents",
                    agent.id,
                    agent.name,
                    owner,
                )
            )

        def agent_uses(agent: AgentDefinition, owner: str):
            for binding in agent.mcp_bindings:
                if binding.enabled:
                    add_agent(
                        agent,
                        owner,
                        "mcp:" + binding.id,
                        "mcp",
                        binding.model_dump(mode="json"),
                    )
            if agent.sandbox_ref in sandbox:
                add_agent(
                    agent,
                    owner,
                    "sandbox_ref",
                    "sandbox:" + sandbox[agent.sandbox_ref].provider,
                    agent.sandbox_ref,
                )
            if agent.observability_ref:
                add_agent(
                    agent,
                    owner,
                    "observability_ref",
                    "langfuse",
                    agent.observability_ref,
                )

        for agent in config.agents:
            if not agent.enabled:
                continue
            agent_uses(agent, agent.id)
        return uses

    def issues(self, config: WorkspaceConfig) -> list[dict[str, Any]]:
        catalog = self.catalog()
        result = []
        for use in self._uses(config):
            status = self._status(catalog, use.feature)
            # A PostgreSQL resource may have one available driver, but use another.
            unavailable_database = use.resource_type == "databases"
            if status["available"] and not unavailable_database:
                continue
            result.append(
                {
                    "code": "FEATURE_UNAVAILABLE",
                    "feature": use.feature,
                    "message": status["reason"] or "The configured database driver is unavailable",
                    "extra": status["extra"],
                    "resource_type": use.resource_type,
                    "resource_id": use.resource_id,
                    "label": use.label,
                    "agent_id": use.agent_id,
                    "key": use.key,
                }
            )
        return result

    def validate_transition(self, current: WorkspaceConfig, candidate: WorkspaceConfig) -> None:
        before = {use.key: use for use in self._uses(current)}
        after = {use.key: use for use in self._uses(candidate)}
        for issue in self.issues(candidate):
            new = after[issue["key"]]
            old = before.get(new.key)
            if old is not None and old.feature == new.feature:
                if new.value == old.value:
                    continue
                if isinstance(old.value, dict) and new.value == {**old.value, "enabled": False}:
                    continue
            raise ResourceCapabilityError(
                "Cannot configure an unavailable feature; install its adapter first",
                details=issue,
            )

    def require_agent(self, config: WorkspaceConfig, agent_id: str) -> None:
        issues = [
            issue
            for issue in self.issues(config)
            if issue["agent_id"] == agent_id or issue["key"] == "runtime.driver"
        ]
        if issues:
            raise AgentNotReadyError(
                "Agent requires unavailable adapters", details={"issues": issues}
            )

    def require(self, feature: str) -> None:
        status = self._status(self.catalog(), feature)
        if not status["available"]:
            raise ResourceCapabilityError(
                "This operation requires an unavailable adapter",
                details={"feature": feature, **status},
            )
