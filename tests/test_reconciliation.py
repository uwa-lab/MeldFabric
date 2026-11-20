from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from meldfabric import register
from meldfabric.config.defaults import create_default_config
from meldfabric.config.models import (
    AgentDefinition,
    GraphEdge,
    GraphNode,
    GraphOrchestration,
    HookBinding,
    HookResource,
    PublishedEndpoint,
    ToolBinding,
    ToolResource,
)
from meldfabric.config.reconciliation import reconcile_workspace
from meldfabric.registrations import RegistrationRuntime
from meldfabric.registrations.catalog import RegistrationDefinition


def test_registered_tool_gets_one_virtual_configuration_resource() -> None:
    @register.tool(id="tool_reconciliation_virtual", name="reconciliation_virtual")
    def virtual_tool(value: str) -> str:
        return value

    definition = virtual_tool.__meldfabric_agent_tool__
    registration = RegistrationRuntime().get("tool", definition.id)
    assert registration is not None
    raw = create_default_config()

    result = reconcile_workspace(raw, (registration,))

    assert raw.resources.tools == []
    assert len(result.config.resources.tools) == 1
    resource = result.config.resources.tools[0]
    assert resource.id == registration.id
    assert resource.registration_digest == registration.digest


def test_missing_tool_is_removed_and_impact_propagates_to_endpoint() -> None:
    raw = create_default_config()
    raw.resources.tools = [
        ToolResource(id="tool_removed_code", name="removed_code", registration_digest="old")
    ]
    raw.agents = [
        AgentDefinition(
            id="agent_affected",
            name="affected",
            tool_bindings=[ToolBinding(id="binding_removed_tool", tool_ref="tool_removed_code")],
        )
    ]
    raw.endpoints = [
        PublishedEndpoint(
            id="endpoint_affected",
            name="affected-endpoint",
            target_ref="agent_affected",
        )
    ]
    raw = type(raw).model_validate(raw.model_dump(mode="json"))

    result = reconcile_workspace(raw, ())

    assert result.config.resources.tools == []
    assert result.config.agents[0].tool_bindings == []
    issue = result.report.issues[0]
    assert issue.code == "REGISTRATION_MISSING"
    assert issue.subject_name == "removed_code"
    assert issue.severity == "critical"
    assert {effect.entity_id for effect in issue.effects} >= {
        "agent_affected",
        "endpoint_affected",
    }


def test_same_registration_id_normalizes_config_and_updates_digest() -> None:
    class CurrentConfig(BaseModel):
        model_config = ConfigDict(extra="ignore")

        current: str = "default"

    def callback(_event) -> None:
        return None

    definition = RegistrationDefinition(
        id="hook_stable_contract",
        kind="hook",
        name="stable_contract",
        description="current contract",
        python_path="tests:callback",
        module="tests",
        target=callback,
        config_model=CurrentConfig,
    )
    raw = create_default_config()
    raw.resources.hooks = [
        HookResource(
            id="hook_config_instance",
            name="stable-hook",
            registration_id=definition.id,
            registration_digest="old-digest",
            config={"removed": True},
        )
    ]
    raw.agents = [
        AgentDefinition(
            id="agent_hook",
            name="hook-agent",
            hook_bindings=[HookBinding(id="binding_hook", hook_ref="hook_config_instance")],
        )
    ]
    raw = type(raw).model_validate(raw.model_dump(mode="json"))

    result = reconcile_workspace(raw, (definition,))

    resource = result.config.resources.hooks[0]
    assert resource.config == {"current": "default"}
    assert resource.registration_digest == definition.digest
    assert {issue.code for issue in result.report.issues} == {
        "REGISTRATION_CHANGED",
        "CONFIG_NORMALIZED",
    }
    normalized = next(issue for issue in result.report.issues if issue.code == "CONFIG_NORMALIZED")
    assert normalized.details["removed_fields"] == ["removed"]
    assert normalized.details["defaulted_fields"] == ["current"]


def test_missing_graph_condition_becomes_unconditional_and_is_critical() -> None:
    raw = create_default_config()
    raw.agents = [
        AgentDefinition(id="agent_graph_a", name="graph-a"),
        AgentDefinition(id="agent_graph_b", name="graph-b"),
    ]
    raw.orchestrations = [
        GraphOrchestration(
            id="graph_affected",
            name="affected-graph",
            nodes=[
                GraphNode(id="node_graph_a", target_ref="agent_graph_a"),
                GraphNode(id="node_graph_b", target_ref="agent_graph_b"),
            ],
            edges=[
                GraphEdge(
                    id="edge_missing_condition",
                    source="node_graph_a",
                    target="node_graph_b",
                    condition_registration_id="condition_removed_code",
                    condition_registration_digest="old",
                    condition_config={"field": "value"},
                )
            ],
            entry_points=["node_graph_a"],
        )
    ]
    raw.endpoints = [
        PublishedEndpoint(
            id="endpoint_graph",
            name="graph-endpoint",
            target_type="orchestration",
            target_ref="graph_affected",
        )
    ]
    raw = type(raw).model_validate(raw.model_dump(mode="json"))

    result = reconcile_workspace(raw, ())

    edge = result.config.orchestrations[0].edges[0]
    assert edge.condition_registration_id is None
    assert edge.condition_registration_digest is None
    assert edge.condition_config == {}
    issue = result.report.issues[0]
    assert issue.severity == "critical"
    assert {effect.entity_id for effect in issue.effects} >= {
        "graph_affected",
        "endpoint_graph",
    }
