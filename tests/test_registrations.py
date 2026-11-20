from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from strands import Agent
from strands.hooks import BeforeToolCallEvent, HookRegistry
from strands.interventions import Deny, Proceed

from meldfabric import (
    hook_callback,
    intervention_callback,
    plugin_hook,
    plugin_tool,
    register,
)
from meldfabric.agents.native_compiler import _RestoreConfiguredPrompt
from meldfabric.config.models import (
    AgentDefinition,
    HookResource,
    RegisteredInterventionResource,
    RegisteredPluginResource,
)
from meldfabric.errors import CodeRegistrationError
from meldfabric.identity import UserIdentity
from meldfabric.registrations import RegistrationRuntime
from tests.support.strands_model import ScriptedStrandsModel


class ExampleConfig(BaseModel):
    prefix: str = "registered"


def runtime_inputs():
    workspace = SimpleNamespace(workspace=SimpleNamespace(id="workspace-test"))
    agent = AgentDefinition(id="agent_registration", name="registration-agent")
    runtime = {
        "user_id": "user-7",
        "thread_id": "thread-7",
        "run_id": "run-7",
        "endpoint_id": "endpoint-7",
        "identity": UserIdentity("user-7", {"tenant_id": "tenant-7"}),
    }
    return workspace, agent, runtime


def test_register_namespace_owns_tool_registration() -> None:
    @register.tool(id="tool_registration_namespace", name="registration_namespace")
    def registered(value: str) -> str:
        return value

    assert registered("ok") == "ok"
    import meldfabric
    import meldfabric.registrations as registrations

    assert not hasattr(meldfabric, "tool")
    for public_name in ("plugin_hook", "plugin_tool", "register"):
        assert public_name in vars(meldfabric)
        assert public_name in vars(registrations)


@pytest.mark.asyncio
async def test_standalone_hook_and_hook_group_are_materialized() -> None:
    seen: list[str] = []

    @register.hook(name="registration_standalone_hook", config_model=ExampleConfig)
    async def standalone(event: BeforeToolCallEvent, *, context) -> None:
        seen.append(f"{context.config.prefix}:{event.tool_use['name']}")

    @register.hook_group(name="registration_hook_group", config_model=ExampleConfig)
    class AuditHooks:
        def __init__(self, context):
            self.context = context

        @hook_callback(priority=-5)
        def record(self, event: BeforeToolCallEvent) -> None:
            seen.append(f"group:{self.context.user_id}:{event.tool_use['name']}")

    workspace, agent, runtime = runtime_inputs()
    registry = RegistrationRuntime()
    event = BeforeToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=None,
        tool_use={"name": "lookup", "toolUseId": "call-1", "input": {}},
        invocation_state={},
    )
    for target in (standalone, AuditHooks):
        registration = target.__meldfabric_registration__
        name = registration.name
        resource = HookResource(
            id=f"hook_{name}",
            name=name,
            registration_id=registration.id,
            config={"prefix": "audit"},
        )
        provider = registry.build_hook(
            registration.id,
            config=resource.config,
            resource=resource,
            agent=agent,
            workspace=workspace,
            runtime=runtime,
        )
        hooks = HookRegistry()
        provider.register_hooks(hooks)
        await hooks.invoke_callbacks_async(event)

    assert seen == ["audit:lookup", "group:user-7:lookup"]


@pytest.mark.asyncio
async def test_function_and_group_interventions_return_native_actions() -> None:
    @register.intervention(name="registration_function_policy", on_error="deny")
    def function_policy(event: BeforeToolCallEvent):
        if event.tool_use["name"] == "delete_file":
            return Deny(reason="blocked")
        return Proceed()

    @register.intervention_group(name="registration_group_policy")
    class GroupPolicy:
        def __init__(self, context):
            self.context = context

        @intervention_callback
        def decide(self, event: BeforeToolCallEvent):
            return Deny(reason=self.context.user_id)

    workspace, agent, runtime = runtime_inputs()
    registry = RegistrationRuntime()
    event = BeforeToolCallEvent(
        agent=SimpleNamespace(),
        selected_tool=None,
        tool_use={"name": "delete_file", "toolUseId": "call-2", "input": {}},
        invocation_state={},
    )
    for target, reason in (
        (function_policy, "blocked"),
        (GroupPolicy, "user-7"),
    ):
        registration = target.__meldfabric_registration__
        name = registration.name
        resource = RegisteredInterventionResource(
            id=f"intervention_{name}",
            name=name,
            registration_id=registration.id,
            on_error="deny",
        )
        handler = registry.build_intervention(
            registration.id,
            config=resource.config,
            resource=resource,
            agent=agent,
            workspace=workspace,
            runtime=runtime,
            on_error=resource.on_error,
        )
        action = await handler.before_tool_call(event)
        assert isinstance(action, Deny)
        assert action.reason == reason


@pytest.mark.asyncio
async def test_registered_plugin_initializes_agent_and_supplies_hook_and_tool() -> None:
    lifecycle: list[str] = []

    @register.plugin(name="registration_context_plugin", config_model=ExampleConfig)
    class ContextPlugin:
        def __init__(self, context):
            self.context = context

        async def init_agent(self, agent) -> None:
            agent.system_prompt = f"{agent.system_prompt}\n{self.context.config.prefix}"
            agent.state.set("tenant_id", self.context.identity.attributes["tenant_id"])

        @plugin_hook
        def before_tool(self, event: BeforeToolCallEvent) -> None:
            event.invocation_state["plugin_hook"] = self.context.user_id

        @plugin_tool(name="registration_plugin_echo", description="Echo through plugin")
        def echo(self, value: str) -> str:
            return f"{self.context.config.prefix}:{value}"

        async def close(self) -> None:
            lifecycle.append("closed")

    workspace, definition, runtime = runtime_inputs()
    resource = RegisteredPluginResource(
        id="plugin_registration_context",
        name="context-plugin",
        registration_id=ContextPlugin.__meldfabric_registration__.id,
        config={"prefix": "tenant"},
    )
    plugin = RegistrationRuntime().build_plugin(
        resource.registration_id,
        config=resource.config,
        resource=resource,
        agent=definition,
        workspace=workspace,
        runtime=runtime,
    )
    model = ScriptedStrandsModel(
        lambda _messages, _tools, index: (
            {
                "tool": {
                    "name": "registration_plugin_echo",
                    "input": {"value": "hello"},
                }
            }
            if index == 1
            else "finished"
        )
    )
    agent = await asyncio.to_thread(
        Agent,
        model=model,
        system_prompt="base",
        plugins=[plugin],
        hooks=[_RestoreConfiguredPrompt("base")],
        callback_handler=None,
    )
    result = await agent.invoke_async("run plugin")
    assert str(result).strip() == "finished"
    assert agent.system_prompt.endswith("tenant")
    assert agent.state.get("tenant_id") == "tenant-7"
    assert any(tool["name"] == "registration_plugin_echo" for tool in model.calls[0]["tools"])
    assert model.calls[1]["invocation_state"]["plugin_hook"] == "user-7"
    await plugin.close()
    assert lifecycle == ["closed"]


def test_intervention_group_rejects_duplicate_event_callbacks() -> None:
    with pytest.raises(CodeRegistrationError, match="at most one callback"):

        @register.intervention_group(name="registration_duplicate_policy")
        class DuplicatePolicy:
            @intervention_callback
            def first(self, event: BeforeToolCallEvent):
                return Proceed()

            @intervention_callback
            def second(self, event: BeforeToolCallEvent):
                return Proceed()


def test_registration_rejects_ambiguous_class_and_condition_signatures() -> None:
    with pytest.raises(CodeRegistrationError, match="named 'context'"):

        @register.hook_group(name="registration_bad_constructor")
        class BadConstructor:
            def __init__(self, settings):
                self.settings = settings

            @hook_callback
            def callback(self, event: BeforeToolCallEvent) -> None:
                return None

    with pytest.raises(CodeRegistrationError, match="state as the first"):

        @register.graph_condition(name="registration_bad_condition")
        def bad_condition(context) -> bool:
            return bool(context)


def test_graph_condition_adapter_exposes_context_aware_strands_signature() -> None:
    @register.graph_condition(
        id="registration_graph_context_adapter",
        name="registration_graph_context_adapter",
    )
    def condition(state, context) -> bool:
        return context.source_node_id in state.results

    workspace = SimpleNamespace(workspace=SimpleNamespace(id="workspace-test"))
    orchestration = SimpleNamespace(id="orchestration-test")
    edge = SimpleNamespace(
        id="edge-test",
        source="source-node",
        target="target-node",
        condition_config={},
    )
    adapter = RegistrationRuntime().build_graph_condition(
        condition.__meldfabric_registration__.id,
        edge=edge,
        orchestration=orchestration,
        workspace=workspace,
    )

    assert "invocation_state" in inspect.signature(adapter).parameters
    assert adapter(
        SimpleNamespace(results={"source-node": object()}),
        invocation_state={},
    )


def test_member_decorators_are_scoped_to_their_container_type() -> None:
    with pytest.raises(CodeRegistrationError, match="not valid"):

        @register.plugin(name="registration_wrong_plugin_marker")
        class WrongPluginMarker:
            @hook_callback
            def callback(self, event: BeforeToolCallEvent) -> None:
                return None

    with pytest.raises(CodeRegistrationError, match="not valid"):

        @register.hook_group(name="registration_wrong_group_marker")
        class WrongGroupMarker:
            @plugin_hook
            def callback(self, event: BeforeToolCallEvent) -> None:
                return None

    with pytest.raises(CodeRegistrationError, match="cannot use requires_sandbox"):

        @plugin_tool(requires_sandbox=True)
        def unsafe_plugin_method(self, value: str) -> str:
            return value


def test_display_names_are_not_registration_identity() -> None:
    @register.hook(name="registration_shared_hook_name")
    def standalone(event: BeforeToolCallEvent) -> None:
        return None

    @register.hook_group(name="registration_shared_hook_name")
    class SameNameHooks:
        @hook_callback
        def callback(self, event: BeforeToolCallEvent) -> None:
            return None

    @register.intervention(name="registration_shared_intervention_name")
    def policy(event: BeforeToolCallEvent):
        return Proceed()

    @register.intervention_group(name="registration_shared_intervention_name")
    class SameNameInterventions:
        @intervention_callback
        def callback(self, event: BeforeToolCallEvent):
            return Proceed()

    assert standalone.__meldfabric_registration__.id != SameNameHooks.__meldfabric_registration__.id
    assert (
        policy.__meldfabric_registration__.id
        != SameNameInterventions.__meldfabric_registration__.id
    )


def test_registration_ids_are_global_across_kinds() -> None:
    @register.hook(id="global_registration_identity", name="global_identity_hook")
    def standalone(event: BeforeToolCallEvent) -> None:
        return None

    with pytest.raises(CodeRegistrationError, match="already owned"):

        @register.intervention(
            id="global_registration_identity",
            name="global_identity_intervention",
        )
        def policy(event: BeforeToolCallEvent):
            return Proceed()
