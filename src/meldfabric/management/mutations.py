"""Reference-safe destructive mutations for the Strands workspace graph."""

from __future__ import annotations

from typing import Any

from meldfabric.config.models import WorkspaceConfig
from meldfabric.errors import ConfigurationError


def deletion_patch(
    config: WorkspaceConfig,
    collection: str,
    entity_id: str,
) -> tuple[dict[str, Any], set[str]]:
    """Delete an entity and detach every configuration edge that points to it.

    Durable session and memory records deliberately remain in their storage database.
    Endpoint credentials are not part of the configuration graph and are revoked by the
    caller after the new generation has been committed.
    """

    if collection == "tools":
        raise ConfigurationError("Code-managed tools can be disabled but not deleted")

    document = config.model_dump(mode="json")
    resources: dict[str, list[dict[str, Any]]] = document["resources"]
    top_level = {"agents", "orchestrations", "endpoints"}
    if collection not in top_level and collection not in resources:
        raise ConfigurationError("Unknown resource collection", details={"collection": collection})
    values = document[collection] if collection in top_level else resources[collection]
    if not any(item["id"] == entity_id for item in values):
        raise ConfigurationError(
            "Entity does not exist",
            details={"collection": collection, "entity_id": entity_id},
        )

    removed: set[str] = {entity_id}
    values[:] = [item for item in values if item["id"] != entity_id]

    # A provider owns its discovered model registrations. An AgentSkills or context
    # offloader plugin becomes invalid when its final source or database disappears.
    if collection == "model_providers":
        owned = {item["id"] for item in resources["models"] if item["provider_ref"] == entity_id}
        removed.update(owned)
        resources["models"][:] = [item for item in resources["models"] if item["id"] not in owned]

    if collection == "skill_sources":
        for plugin in resources["plugins"]:
            if plugin.get("kind") == "agent_skills":
                plugin["skill_source_refs"] = [
                    ref for ref in plugin["skill_source_refs"] if ref != entity_id
                ]
        empty_plugins = {
            item["id"]
            for item in resources["plugins"]
            if item.get("kind") == "agent_skills" and not item["skill_source_refs"]
        }
        removed.update(empty_plugins)
        resources["plugins"][:] = [
            item for item in resources["plugins"] if item["id"] not in empty_plugins
        ]

    if collection == "databases":
        invalid_plugins = {
            item["id"]
            for item in resources["plugins"]
            if item.get("kind") == "context_offloader" and item["database_ref"] == entity_id
        }
        removed.update(invalid_plugins)
        resources["plugins"][:] = [
            item for item in resources["plugins"] if item["id"] not in invalid_plugins
        ]

    removed_agents = {entity_id} if collection == "agents" else set()
    _detach_agent_resources(document["agents"], removed)

    removed_orchestrations = {entity_id} if collection == "orchestrations" else set()
    kept_orchestrations: list[dict[str, Any]] = []
    for item in document["orchestrations"]:
        normalized = _detach_orchestration(item, removed_agents, removed)
        if normalized is None:
            removed_orchestrations.add(item["id"])
            removed.add(item["id"])
        else:
            kept_orchestrations.append(normalized)
    document["orchestrations"] = kept_orchestrations

    removed_endpoints = {entity_id} if collection == "endpoints" else set()
    kept_endpoints = []
    for item in document["endpoints"]:
        target_removed = (
            item["target_type"] == "agent" and item["target_ref"] in removed_agents
        ) or (
            item["target_type"] == "orchestration" and item["target_ref"] in removed_orchestrations
        )
        if target_removed:
            removed_endpoints.add(item["id"])
            removed.add(item["id"])
        else:
            kept_endpoints.append(item)
    document["endpoints"] = kept_endpoints

    # Validate the complete result before returning a patch so no partial cascade can be
    # committed. config_generation is assigned by the repository during the mutation.
    WorkspaceConfig.model_validate(document)
    return {
        "resources": document["resources"],
        "agents": document["agents"],
        "orchestrations": document["orchestrations"],
        "endpoints": document["endpoints"],
    }, removed_endpoints


def _detach_agent_resources(agents: list[dict[str, Any]], removed: set[str]) -> None:
    scalar_refs = ("model_ref", "sandbox_ref", "observability_ref")
    binding_refs = {
        "tool_bindings": "tool_ref",
        "mcp_bindings": "mcp_ref",
        "remote_tool_bindings": "remote_agent_ref",
        "plugin_bindings": "plugin_ref",
        "hook_bindings": "hook_ref",
        "intervention_bindings": "intervention_ref",
    }
    for agent in agents:
        for field in scalar_refs:
            if agent.get(field) in removed:
                agent[field] = None
        for field in ("session_manager", "memory_manager"):
            if (agent.get(field) or {}).get("database_ref") in removed:
                agent[field] = None
        for field, reference_field in binding_refs.items():
            agent[field] = [
                binding for binding in agent[field] if binding[reference_field] not in removed
            ]


def _detach_orchestration(
    item: dict[str, Any],
    removed_agents: set[str],
    removed_resources: set[str],
) -> dict[str, Any] | None:
    session = item.get("session_manager")
    if session and session.get("database_ref") in removed_resources:
        item["session_manager"] = None

    kind = item["kind"]
    if kind == "agent_as_tool":
        if item["coordinator_ref"] in removed_agents:
            return None
        item["members"] = [
            member for member in item["members"] if member["agent_ref"] not in removed_agents
        ]
        return item
    if kind == "swarm":
        item["agent_refs"] = [ref for ref in item["agent_refs"] if ref not in removed_agents]
        if len(item["agent_refs"]) < 2:
            return None
        if item.get("entry_agent_ref") not in item["agent_refs"]:
            item["entry_agent_ref"] = item["agent_refs"][0]
        return item
    if kind == "graph":
        item["nodes"] = [
            node
            for node in item["nodes"]
            if not (
                (node["target_type"] == "agent" and node["target_ref"] in removed_agents)
                or (
                    node["target_type"] == "remote_agent"
                    and node["target_ref"] in removed_resources
                )
            )
        ]
        if not item["nodes"]:
            return None
        node_ids = {node["id"] for node in item["nodes"]}
        local_node_ids = [node["id"] for node in item["nodes"] if node["target_type"] == "agent"]
        if not local_node_ids:
            return None
        item["edges"] = [
            edge
            for edge in item["edges"]
            if edge["source"] in node_ids and edge["target"] in node_ids
        ]
        item["entry_points"] = [entry for entry in item["entry_points"] if entry in node_ids]
        if not item["entry_points"]:
            item["entry_points"] = [local_node_ids[0]]
        if not any(entry in local_node_ids for entry in item["entry_points"]):
            item["entry_points"].insert(0, local_node_ids[0])
        if (
            item.get("primary_entry_point") not in item["entry_points"]
            or item.get("primary_entry_point") not in local_node_ids
        ):
            item["primary_entry_point"] = next(
                entry for entry in item["entry_points"] if entry in local_node_ids
            )
        return item
    if kind == "workflow":
        item["tasks"] = [task for task in item["tasks"] if task["agent_ref"] not in removed_agents]
        if not item["tasks"]:
            return None
        task_ids = {task["id"] for task in item["tasks"]}
        for task in item["tasks"]:
            task["dependency_refs"] = [ref for ref in task["dependency_refs"] if ref in task_ids]
        root_ids = [task["id"] for task in item["tasks"] if not task["dependency_refs"]]
        if item.get("primary_task_ref") not in root_ids:
            item["primary_task_ref"] = root_ids[0]
        return item
    raise ConfigurationError("Unsupported orchestration kind", details={"kind": kind})
