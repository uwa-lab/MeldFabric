from __future__ import annotations

import pytest
from pydantic import ValidationError

from meldfabric.config.defaults import create_default_config
from meldfabric.config.models import (
    AgentDefinition,
    CanvasPosition,
    GraphEdge,
    GraphNode,
    GraphOrchestration,
    HumanInTheLoopInterventionResource,
    InterventionBinding,
    RemoteToolBinding,
    ResourcesConfig,
    ToolBinding,
    WorkflowOrchestration,
    WorkflowTask,
    WorkspaceConfig,
)
from meldfabric.management.mutations import deletion_patch


def test_per_call_remote_session_cannot_request_resumable_interaction() -> None:
    with pytest.raises(ValidationError, match="per_call"):
        RemoteToolBinding(
            remote_agent_ref="remote_one",
            session_policy="per_call",
            interaction_policy="bubble_to_parent",
        )


def test_code_registered_tool_binding_does_not_require_a_persisted_resource_row() -> None:
    base = create_default_config()
    config = WorkspaceConfig(
        workspace=base.workspace,
        resources=base.resources,
        agents=[
            AgentDefinition(
                id="agent_one",
                name="one",
                tool_bindings=[ToolBinding(tool_ref="tool_from_decorator")],
            )
        ],
    )
    assert config.agents[0].tool_bindings[0].tool_ref == "tool_from_decorator"


def test_human_in_the_loop_requires_native_session_manager() -> None:
    base = create_default_config()
    intervention = HumanInTheLoopInterventionResource(
        id="intervention_approval",
        name="approval",
    )
    with pytest.raises(ValidationError, match="session manager"):
        WorkspaceConfig(
            workspace=base.workspace,
            resources=ResourcesConfig(
                databases=base.resources.databases,
                interventions=[intervention],
            ),
            agents=[
                AgentDefinition(
                    id="agent_one",
                    name="one",
                    intervention_bindings=[InterventionBinding(intervention_ref=intervention.id)],
                )
            ],
        )


def test_published_graph_requires_a_local_primary_entry() -> None:
    with pytest.raises(ValidationError, match="primary entry point must be a local Agent"):
        GraphOrchestration(
            id="orch_graph",
            name="graph",
            nodes=[
                GraphNode(
                    id="node_remote",
                    target_type="remote_agent",
                    target_ref="remote_one",
                )
            ],
            entry_points=["node_remote"],
        )


def test_graph_allows_multiple_entries_repeated_agents_cycles_and_positions() -> None:
    graph = GraphOrchestration(
        id="orch_graph",
        name="graph",
        nodes=[
            GraphNode(
                id="node_a",
                target_ref="agent_one",
                position=CanvasPosition(x=-120, y=48),
            ),
            GraphNode(id="node_b", target_ref="agent_one"),
            GraphNode(id="node_remote", target_type="remote_agent", target_ref="remote_one"),
        ],
        edges=[
            GraphEdge(id="edge_ab", source="node_a", target="node_b"),
            GraphEdge(id="edge_ba", source="node_b", target="node_a"),
        ],
        entry_points=["node_a", "node_b", "node_remote"],
    )

    assert graph.nodes[0].position == CanvasPosition(x=-120, y=48)
    assert graph.nodes[0].target_ref == graph.nodes[1].target_ref
    assert len(graph.entry_points) == 3
    assert graph.primary_entry_point == "node_a"


def test_graph_primary_entry_must_be_a_local_start_node() -> None:
    with pytest.raises(ValidationError, match="must be one of entry_points"):
        GraphOrchestration(
            id="orch_graph",
            name="graph",
            nodes=[
                GraphNode(id="node_a", target_ref="agent_one"),
                GraphNode(id="node_b", target_ref="agent_two"),
            ],
            entry_points=["node_a"],
            primary_entry_point="node_b",
        )

    with pytest.raises(ValidationError, match="must be a local Agent"):
        GraphOrchestration(
            id="orch_graph",
            name="graph",
            nodes=[
                GraphNode(id="node_a", target_ref="agent_one"),
                GraphNode(
                    id="node_remote",
                    target_type="remote_agent",
                    target_ref="remote_one",
                ),
            ],
            entry_points=["node_a", "node_remote"],
            primary_entry_point="node_remote",
        )


@pytest.mark.parametrize(
    "edges, message",
    [
        ([GraphEdge(source="node_a", target="node_a")], "cannot connect"),
        (
            [
                GraphEdge(source="node_a", target="node_b"),
                GraphEdge(source="node_a", target="node_b"),
            ],
            "must be unique",
        ),
    ],
)
def test_graph_rejects_self_and_duplicate_edges(edges: list[GraphEdge], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        GraphOrchestration(
            id="orch_graph",
            name="graph",
            nodes=[
                GraphNode(id="node_a", target_ref="agent_one"),
                GraphNode(id="node_b", target_ref="agent_one"),
            ],
            edges=edges,
            entry_points=["node_a"],
        )


def test_workflow_allows_multiple_roots_repeated_agents_and_positions() -> None:
    workflow = WorkflowOrchestration(
        id="orch_workflow",
        name="workflow",
        tasks=[
            WorkflowTask(
                id="task_a",
                agent_ref="agent_one",
                instruction="A",
                position=CanvasPosition(x=10, y=20),
            ),
            WorkflowTask(id="task_b", agent_ref="agent_one", instruction="B"),
            WorkflowTask(
                id="task_c",
                agent_ref="agent_one",
                instruction="C",
                dependency_refs=["task_a", "task_b"],
            ),
        ],
    )

    assert workflow.tasks[0].agent_ref == workflow.tasks[1].agent_ref
    assert [task.id for task in workflow.tasks if not task.dependency_refs] == [
        "task_a",
        "task_b",
    ]
    assert workflow.tasks[0].position == CanvasPosition(x=10, y=20)
    assert workflow.primary_task_ref == "task_a"


def test_workflow_primary_task_must_be_a_root() -> None:
    with pytest.raises(ValidationError, match="must be a start task"):
        WorkflowOrchestration(
            id="orch_workflow",
            name="workflow",
            tasks=[
                WorkflowTask(id="task_a", agent_ref="agent_one", instruction="A"),
                WorkflowTask(
                    id="task_b",
                    agent_ref="agent_two",
                    instruction="B",
                    dependency_refs=["task_a"],
                ),
            ],
            primary_task_ref="task_b",
        )


def test_agent_deletion_reassigns_graph_and_workflow_primary_entries() -> None:
    base = create_default_config()
    config = WorkspaceConfig(
        workspace=base.workspace,
        resources=base.resources,
        agents=[
            AgentDefinition(id="agent_one", name="one"),
            AgentDefinition(id="agent_two", name="two"),
        ],
        orchestrations=[
            GraphOrchestration(
                id="orch_graph",
                name="graph",
                nodes=[
                    GraphNode(id="node_one", target_ref="agent_one"),
                    GraphNode(id="node_two", target_ref="agent_two"),
                ],
                entry_points=["node_one", "node_two"],
                primary_entry_point="node_one",
            ),
            WorkflowOrchestration(
                id="orch_workflow",
                name="workflow",
                tasks=[
                    WorkflowTask(id="task_one", agent_ref="agent_one", instruction="one"),
                    WorkflowTask(id="task_two", agent_ref="agent_two", instruction="two"),
                ],
                primary_task_ref="task_one",
            ),
        ],
    )

    patch, _ = deletion_patch(config, "agents", "agent_one")
    updated = WorkspaceConfig.model_validate(
        config.model_dump(mode="json") | patch,
    )

    graph = next(item for item in updated.orchestrations if item.id == "orch_graph")
    workflow = next(item for item in updated.orchestrations if item.id == "orch_workflow")
    assert graph.primary_entry_point == "node_two"
    assert graph.entry_points == ["node_two"]
    assert workflow.primary_task_ref == "task_two"


@pytest.mark.parametrize(
    "tasks, message",
    [
        (
            [
                WorkflowTask(
                    id="task_a",
                    agent_ref="agent_one",
                    instruction="A",
                    dependency_refs=["task_a"],
                )
            ],
            "cannot depend on themselves",
        ),
        (
            [
                WorkflowTask(id="task_a", agent_ref="agent_one", instruction="A"),
                WorkflowTask(
                    id="task_b",
                    agent_ref="agent_one",
                    instruction="B",
                    dependency_refs=["task_a", "task_a"],
                ),
            ],
            "must be unique",
        ),
        (
            [
                WorkflowTask(
                    id="task_a",
                    agent_ref="agent_one",
                    instruction="A",
                    dependency_refs=["task_b"],
                ),
                WorkflowTask(
                    id="task_b",
                    agent_ref="agent_one",
                    instruction="B",
                    dependency_refs=["task_a"],
                ),
            ],
            "must form a DAG",
        ),
    ],
)
def test_workflow_rejects_invalid_dependencies(tasks: list[WorkflowTask], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowOrchestration(id="orch_workflow", name="workflow", tasks=tasks)
