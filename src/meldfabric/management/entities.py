"""Typed entity adaptation and mutation helpers for the management plane."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

from pydantic import TypeAdapter, ValidationError

from meldfabric.config.models import (
    AgentDefinition,
    DatabaseConnection,
    GraphOrchestration,
    HookResource,
    InterventionResource,
    MCPServer,
    ModelProvider,
    ModelResource,
    ObservabilityProvider,
    OrchestrationDefinition,
    PluginResource,
    PublishedEndpoint,
    RegisteredInterventionResource,
    RegisteredPluginResource,
    RemoteAgent,
    SandboxProfile,
    SkillSource,
    ToolResource,
)
from meldfabric.errors import ConfigurationError

if TYPE_CHECKING:
    from meldfabric.app import AgentKit


RESOURCE_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "databases": TypeAdapter(DatabaseConnection),
    "model_providers": TypeAdapter(ModelProvider),
    "models": TypeAdapter(ModelResource),
    "tools": TypeAdapter(ToolResource),
    "mcp_servers": TypeAdapter(MCPServer),
    "skill_sources": TypeAdapter(SkillSource),
    # Pydantic accepts Annotated discriminated unions here, while its static
    # signature models concrete runtime classes. Keep that boundary explicit.
    "plugins": TypeAdapter(cast(Any, PluginResource)),
    "hooks": TypeAdapter(HookResource),
    "interventions": TypeAdapter(cast(Any, InterventionResource)),
    "sandbox_profiles": TypeAdapter(SandboxProfile),
    "observability_providers": TypeAdapter(ObservabilityProvider),
    "remote_agents": TypeAdapter(RemoteAgent),
}
AGENT_ADAPTER = TypeAdapter(AgentDefinition)
ORCHESTRATION_ADAPTER: TypeAdapter[Any] = TypeAdapter(cast(Any, OrchestrationDefinition))
ENDPOINT_ADAPTER = TypeAdapter(PublishedEndpoint)

ENTITY_ID_PREFIXES = {
    "agents": "agent",
    "orchestrations": "orch",
    "endpoints": "endpoint",
    "databases": "db",
    "model_providers": "provider",
    "models": "model",
    "mcp_servers": "mcp",
    "skill_sources": "skills",
    "plugins": "plugin",
    "hooks": "hook",
    "interventions": "intervention",
    "sandbox_profiles": "sandbox",
    "observability_providers": "observability",
    "remote_agents": "remote",
}


def validate_registered_resource(
    agent_kit: AgentKit,
    entity: Any,
    *,
    allow_unavailable: bool = False,
) -> None:
    kind: str | None = None
    if isinstance(entity, RegisteredPluginResource):
        kind = "plugin"
    elif isinstance(entity, HookResource):
        kind = "hook"
    elif isinstance(entity, RegisteredInterventionResource):
        kind = "intervention"
    if kind is None:
        return
    definition = agent_kit.registrations.get(kind, entity.registration_id)
    if definition is None:
        if allow_unavailable:
            return
        raise ConfigurationError(
            f"{kind} registration {entity.registration_id!r} is not available in this process",
            details={"kind": kind, "registration_id": entity.registration_id},
        )
    try:
        definition.config_model.model_validate(entity.config)
    except ValidationError as exc:
        raise ConfigurationError(
            f"configuration for {kind} registration {entity.registration_id!r} is invalid",
            details={"errors": exc.errors(include_url=False)},
        ) from exc


def validate_graph_conditions(
    agent_kit: AgentKit,
    entity: Any,
    *,
    allow_unavailable: bool = False,
) -> None:
    if not isinstance(entity, GraphOrchestration):
        return
    for edge in entity.edges:
        if edge.condition_registration_id is None:
            continue
        definition = agent_kit.registrations.get("graph_condition", edge.condition_registration_id)
        if definition is None:
            if allow_unavailable:
                continue
            raise ConfigurationError(
                f"graph condition registration {edge.condition_registration_id!r} is unavailable",
                details={
                    "edge_id": edge.id,
                    "registration_id": edge.condition_registration_id,
                },
            )
        try:
            definition.config_model.model_validate(edge.condition_config)
        except ValidationError as exc:
            raise ConfigurationError(
                f"configuration for graph condition {edge.condition_registration_id!r} is invalid",
                details={"edge_id": edge.id, "errors": exc.errors(include_url=False)},
            ) from exc


def entity_patch(collection: str, values: list[Any]) -> dict[str, Any]:
    dumped = [item.model_dump(mode="json") for item in values]
    return (
        {collection: dumped}
        if collection in {"agents", "orchestrations", "endpoints"}
        else {"resources": {collection: dumped}}
    )


def replace_entity(values: list[Any], entity: Any) -> list[Any]:
    result = [entity if item.id == entity.id else item for item in values]
    if not any(item.id == entity.id for item in values):
        result.append(entity)
    return result


def new_entity_id(collection: str) -> str:
    return f"{ENTITY_ID_PREFIXES[collection]}_{uuid.uuid4().hex}"


__all__ = [
    "AGENT_ADAPTER",
    "ENDPOINT_ADAPTER",
    "ENTITY_ID_PREFIXES",
    "ORCHESTRATION_ADAPTER",
    "RESOURCE_ADAPTERS",
    "entity_patch",
    "new_entity_id",
    "replace_entity",
    "validate_graph_conditions",
    "validate_registered_resource",
]
