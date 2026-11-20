"""Strands-native workspace fixtures used by integration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from meldfabric.config.models import (
    AgentAsToolOrchestration,
    AgentDefinition,
    AgentToolMember,
    GraphEdge,
    GraphNode,
    GraphOrchestration,
    ModelCapabilities,
    ModelProvider,
    ModelResource,
    PublishedEndpoint,
    SwarmOrchestration,
    WorkflowOrchestration,
    WorkflowTask,
)
from tests.support.strands_model import ScriptedModelAdapter, ScriptedStrandsModel

if TYPE_CHECKING:
    from meldfabric import AgentKit


async def configure_strands_workspace(
    kit: AgentKit,
    model: ScriptedStrandsModel,
    *,
    include_orchestrations: bool = True,
) -> dict[str, object]:
    """Install a deterministic model and a representative native workspace."""

    kit.model_adapters.register(ScriptedModelAdapter(model))
    provider = ModelProvider(
        id="provider_scripted",
        name="Scripted",
        adapter="scripted",
        base_url="https://model.invalid/v1",
    )
    model_resource = ModelResource(
        id="model_scripted",
        name="scripted-model",
        provider_ref=provider.id,
        model_name="scripted-model",
        capabilities=ModelCapabilities(tool_calling=True),
    )
    agents = [
        AgentDefinition(
            id=f"agent_{name}",
            name=name,
            description=f"{name} test Agent",
            model_ref=model_resource.id,
        )
        for name in ("coordinator", "researcher", "writer")
    ]
    orchestrations = []
    endpoints = [
        PublishedEndpoint(
            id="endpoint_agent",
            name="agent_endpoint",
            target_ref=agents[0].id,
            protocols=["rest", "ag_ui", "meldfabric_remote", "a2a"],
        )
    ]
    if include_orchestrations:
        orchestrations = [
            AgentAsToolOrchestration(
                id="orch_tools",
                name="agent_tools",
                coordinator_ref=agents[0].id,
                members=[AgentToolMember(agent_ref=agents[1].id)],
            ),
            SwarmOrchestration(
                id="orch_swarm",
                name="native_swarm",
                agent_refs=[agents[0].id, agents[1].id],
                entry_agent_ref=agents[0].id,
            ),
            GraphOrchestration(
                id="orch_graph",
                name="native_graph",
                nodes=[
                    GraphNode(id="node_research", target_ref=agents[1].id),
                    GraphNode(id="node_write", target_ref=agents[2].id),
                ],
                edges=[
                    GraphEdge(
                        id="edge_research_write",
                        source="node_research",
                        target="node_write",
                    )
                ],
                entry_points=["node_research"],
            ),
            WorkflowOrchestration(
                id="orch_workflow",
                name="native_workflow",
                tasks=[
                    WorkflowTask(
                        id="task_research",
                        agent_ref=agents[1].id,
                        instruction="Research the subject",
                    ),
                    WorkflowTask(
                        id="task_write",
                        agent_ref=agents[2].id,
                        instruction="Write the answer",
                        dependency_refs=["task_research"],
                    ),
                ],
            ),
        ]
        endpoints.extend(
            PublishedEndpoint(
                id=f"endpoint_{item.kind}",
                name=f"{item.kind}_endpoint",
                target_type="orchestration",
                target_ref=item.id,
                protocols=["rest", "ag_ui"],
            )
            for item in orchestrations
        )
    loaded = await kit.apatch_config(
        {
            "resources": {
                "model_providers": [provider.model_dump(mode="json")],
                "models": [model_resource.model_dump(mode="json")],
            },
            "agents": [item.model_dump(mode="json") for item in agents],
            "orchestrations": [item.model_dump(mode="json") for item in orchestrations],
            "endpoints": [item.model_dump(mode="json") for item in endpoints],
        }
    )
    await kit.aapply_latest_config(expected_generation=loaded.config.config_generation)
    return {
        "provider": provider,
        "model": model_resource,
        "agents": agents,
        "orchestrations": orchestrations,
        "endpoints": endpoints,
    }
