"""Adapters from MeldFabric code registrations to native Strands runtime objects."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from functools import wraps
from types import MappingProxyType
from typing import Any

from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    AgentInitializedEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.interventions import (
    Confirm,
    Deny,
    Guide,
    InterventionHandler,
    Proceed,
    Transform,
)
from strands.plugins import Plugin

from meldfabric.errors import AgentNotReadyError, CodeRegistrationError
from meldfabric.identity import UserIdentity
from meldfabric.registrations.catalog import (
    MemberDefinition,
    RegistrationDefinition,
    catalog,
)
from meldfabric.registrations.decorators import callback_accepts_context
from meldfabric.registrations.types import (
    GraphConditionContext,
    HookContext,
    InterventionContext,
    PluginAgent,
    PluginContext,
)
from meldfabric.tools.registry import build_strands_tool


def _validated_config(definition: RegistrationDefinition, raw: Mapping[str, Any] | None):
    model = definition.config_model
    if model is None:  # Defensive for registrations created by third-party integrations.
        return None
    try:
        return model.model_validate(dict(raw or {}))
    except Exception as exc:
        raise AgentNotReadyError(
            f"configuration for {definition.kind} registration {definition.name!r} is invalid",
            details={
                "registration_id": definition.id,
                "registration_name": definition.name,
                "python_path": definition.python_path,
            },
        ) from exc


def _runtime_context(
    context_type,
    definition: RegistrationDefinition,
    *,
    raw_config: Mapping[str, Any] | None,
    resource: Any,
    agent: Any,
    workspace: Any,
    runtime: Mapping[str, Any],
):
    user_id = str(runtime.get("user_id") or "anonymous")
    identity = runtime.get("identity")
    if not isinstance(identity, UserIdentity):
        identity = UserIdentity(user_id=user_id)
    return context_type(
        config=_validated_config(definition, raw_config),
        workspace_id=workspace.workspace.id,
        agent_id=agent.id,
        resource_id=resource.id,
        resource_name=resource.name,
        user_id=user_id,
        thread_id=str(runtime.get("thread_id") or "unscoped"),
        identity=identity,
        endpoint_id=runtime.get("endpoint_id"),
        run_id=runtime.get("run_id"),
        trace_attributes=MappingProxyType(dict(runtime.get("trace_attributes") or {})),
    )


def _construct_user_object(definition: RegistrationDefinition, context: Any) -> Any:
    target = definition.target
    parameters = list(inspect.signature(target).parameters.values())
    try:
        if not parameters:
            return target()
        if len(parameters) == 1 and parameters[0].kind not in {
            parameters[0].VAR_POSITIONAL,
            parameters[0].VAR_KEYWORD,
        }:
            return target(context)
    except Exception as exc:
        raise AgentNotReadyError(
            f"failed to initialize registered class {definition.python_path}",
            details={"registration_id": definition.id, "python_path": definition.python_path},
        ) from exc
    raise AgentNotReadyError(
        f"registered class {definition.python_path} must accept no constructor arguments "
        "or one context argument",
        details={"registration_id": definition.id, "python_path": definition.python_path},
    )


async def _invoke_callback(function: Callable[..., Any], event: Any, context: Any) -> Any:
    kwargs = (
        {"context": context}
        if callback_accepts_context(function, method=inspect.ismethod(function))
        else {}
    )
    value = function(event, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _callback_wrapper(function: Callable[..., Any], context: Any) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def invoke_async(event: Any) -> Any:
            return await _invoke_callback(function, event, context)

        return invoke_async

    @wraps(function)
    def invoke_sync(event: Any) -> Any:
        kwargs = (
            {"context": context}
            if callback_accepts_context(function, method=inspect.ismethod(function))
            else {}
        )
        value = function(event, **kwargs)
        if inspect.isawaitable(value):
            raise CodeRegistrationError(
                f"callback {function.__qualname__} returned an awaitable but is not async"
            )
        return value

    return invoke_sync


class _HookGroupAdapter(HookProvider):
    def __init__(
        self,
        callbacks: list[tuple[MemberDefinition, Callable[..., Any]]],
        context: HookContext[Any],
        user_object: Any | None = None,
    ) -> None:
        self.callbacks = callbacks
        self.context = context
        self.user_object = user_object

    def register_hooks(self, registry: HookRegistry, **_kwargs: Any) -> None:
        for member, callback in self.callbacks:
            wrapped = _callback_wrapper(callback, self.context)
            for event_type in member.event_types:
                registry.add_callback(event_type, wrapped, order=member.priority)

    async def close(self) -> None:
        closer = getattr(self.user_object, "close", None)
        if closer is None:
            return
        value = closer()
        if inspect.isawaitable(value):
            await value


class _RegisteredPluginAdapter(Plugin):
    """Native Plugin wrapper around one plain user class instance."""

    def __init__(
        self,
        *,
        runtime_name: str,
        user_object: Any,
        definition: RegistrationDefinition,
        context: PluginContext[Any],
    ) -> None:
        self._runtime_name = runtime_name
        self.user_object = user_object
        self.definition = definition
        self.context = context
        self._meldfabric_tools = []
        for member in definition.members:
            if member.kind != "tool":
                continue
            bound = getattr(user_object, member.attribute)
            tool_definition = member.payload
            # The schema was built from the unbound method with ``self`` excluded; execution
            # must use the bound method belonging to this invocation-scoped plugin object.
            from dataclasses import replace

            bound_definition = replace(tool_definition, function=bound)
            self._meldfabric_tools.append(
                build_strands_tool(
                    bound_definition,
                    name=bound_definition.name,
                    description=bound_definition.description,
                )
            )
        super().__init__()

    @property
    def name(self) -> str:
        return self._runtime_name

    @property
    def hooks(self):
        # Hooks are registered in init_agent so MeldFabric priorities are preserved.
        return []

    @property
    def tools(self):
        return list(self._meldfabric_tools)

    async def init_agent(self, agent: Any) -> None:
        initializer = getattr(self.user_object, "init_agent", None)
        if initializer is not None:
            prompt_before = agent.system_prompt
            value = initializer(PluginAgent(agent))
            if inspect.isawaitable(value):
                await value
            prompt_after = agent.system_prompt
            if prompt_after != prompt_before:

                def restore_plugin_prompt(event: AgentInitializedEvent) -> None:
                    # Snapshot restoration runs on AgentInitializedEvent. Reapply the
                    # plugin-owned prompt mutation afterwards so persisted state cannot
                    # silently erase an explicit init_agent decision.
                    event.agent.system_prompt = prompt_after

                agent.add_hook(restore_plugin_prompt, AgentInitializedEvent)
        for member in self.definition.members:
            if member.kind != "hook":
                continue
            callback = getattr(self.user_object, member.attribute)
            wrapped = _callback_wrapper(callback, self.context)
            for event_type in member.event_types:
                agent.add_hook(wrapped, event_type, order=member.priority)

    async def close(self) -> None:
        closer = getattr(self.user_object, "close", None)
        if closer is None:
            return
        value = closer()
        if inspect.isawaitable(value):
            await value


_INTERVENTION_METHODS: dict[type[Any], str] = {
    BeforeInvocationEvent: "before_invocation",
    BeforeToolCallEvent: "before_tool_call",
    AfterToolCallEvent: "after_tool_call",
    BeforeModelCallEvent: "before_model_call",
    AfterModelCallEvent: "after_model_call",
}
_ALLOWED_ACTIONS: dict[type[Any], tuple[type[Any], ...]] = {
    BeforeInvocationEvent: (Proceed, Deny, Guide, Transform),
    BeforeToolCallEvent: (Proceed, Deny, Guide, Confirm, Transform),
    AfterToolCallEvent: (Proceed, Transform),
    BeforeModelCallEvent: (Proceed, Deny, Guide, Transform),
    AfterModelCallEvent: (Proceed, Guide, Transform),
}


async def _invoke_intervention(
    callback: Callable[..., Any], event: Any, context: InterventionContext[Any]
) -> Any:
    action = await _invoke_callback(callback, event, context)
    allowed = _ALLOWED_ACTIONS[type(event)]
    if not isinstance(action, allowed):
        raise CodeRegistrationError(
            f"intervention callback {callback.__qualname__} returned "
            f"{type(action).__name__}, which is not valid for {type(event).__name__}",
            details={
                "event": type(event).__name__,
                "allowed_actions": [item.__name__ for item in allowed],
            },
        )
    return action


def _intervention_method(callback: Callable[..., Any], context: InterventionContext[Any]):
    @wraps(callback)
    async def method(_self: Any, event: Any, **_kwargs: Any) -> Any:
        return await _invoke_intervention(callback, event, context)

    return method


def _intervention_adapter(
    definition: RegistrationDefinition,
    callbacks: list[tuple[type[Any], Callable[..., Any]]],
    context: InterventionContext[Any],
    *,
    runtime_name: str,
    on_error: str,
) -> InterventionHandler:
    attributes: dict[str, Any] = {
        "name": property(lambda _self: runtime_name),
        "on_error": property(lambda _self: on_error),
        "__module__": definition.module,
    }
    for event_type, callback in callbacks:
        method_name = _INTERVENTION_METHODS.get(event_type)
        if method_name is None:
            raise AgentNotReadyError(
                f"{event_type.__name__} is not an Intervention lifecycle event",
                details={"registration_id": definition.id},
            )
        attributes[method_name] = _intervention_method(callback, context)
    class_name = "_MeldFabricIntervention_" + re.sub(r"[^A-Za-z0-9_]", "_", definition.name)
    adapter_type = type(class_name, (InterventionHandler,), attributes)
    instance = adapter_type()
    vars(instance)["__meldfabric_user_object__"] = getattr(callbacks[0][1], "__self__", None)
    return instance


class RegistrationRuntime:
    """Resolve and materialize process-local registered definitions."""

    @property
    def generation(self) -> int:
        return catalog.generation

    def names(self) -> dict[str, list[str]]:
        return catalog.extension_names()

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        return catalog.catalog()

    def digest(self) -> str:
        return catalog.digest()

    def modules(self) -> tuple[str, ...]:
        return catalog.modules()

    @staticmethod
    def _get(kind: str, registration_id: str) -> RegistrationDefinition | None:
        if kind == "hook":
            return catalog.get("hook", registration_id) or catalog.get(
                "hook_group", registration_id
            )
        if kind == "intervention":
            return catalog.get("intervention", registration_id) or catalog.get(
                "intervention_group", registration_id
            )
        if kind == "tool":
            return catalog.get("tool", registration_id)
        if kind == "plugin":
            return catalog.get("plugin", registration_id)
        if kind == "hook_group":
            return catalog.get("hook_group", registration_id)
        if kind == "intervention_group":
            return catalog.get("intervention_group", registration_id)
        if kind == "graph_condition":
            return catalog.get("graph_condition", registration_id)
        return None

    def get(self, kind: str, registration_id: str) -> RegistrationDefinition | None:
        return self._get(kind, registration_id)

    def by_id(self, registration_id: str) -> RegistrationDefinition | None:
        return catalog.by_id(registration_id)

    def definitions(self) -> tuple[RegistrationDefinition, ...]:
        return catalog.snapshot()[1]

    def build_plugin(
        self,
        registration_id: str,
        *,
        config: Mapping[str, Any],
        resource: Any,
        agent: Any,
        workspace: Any,
        runtime: Mapping[str, Any],
    ) -> Plugin:
        definition = self._get("plugin", registration_id)
        if definition is None:
            raise AgentNotReadyError(f"plugin registration {registration_id!r} is unavailable")
        context = _runtime_context(
            PluginContext,
            definition,
            raw_config=config,
            resource=resource,
            agent=agent,
            workspace=workspace,
            runtime=runtime,
        )
        user_object = _construct_user_object(definition, context)
        runtime_name = f"{definition.name}:{resource.id}"
        return _RegisteredPluginAdapter(
            runtime_name=runtime_name,
            user_object=user_object,
            definition=definition,
            context=context,
        )

    def build_hook(
        self,
        registration_id: str,
        *,
        config: Mapping[str, Any],
        resource: Any,
        agent: Any,
        workspace: Any,
        runtime: Mapping[str, Any],
    ) -> HookProvider:
        definition = self._get("hook", registration_id)
        if definition is None:
            raise AgentNotReadyError(f"hook registration {registration_id!r} is unavailable")
        context = _runtime_context(
            HookContext,
            definition,
            raw_config=config,
            resource=resource,
            agent=agent,
            workspace=workspace,
            runtime=runtime,
        )
        if definition.kind == "hook":
            callbacks = [(definition.members[0], definition.target)]
            user_object = None
        else:
            user_object = _construct_user_object(definition, context)
            callbacks = [
                (member, getattr(user_object, member.attribute)) for member in definition.members
            ]
        return _HookGroupAdapter(callbacks, context, user_object)

    def build_intervention(
        self,
        registration_id: str,
        *,
        config: Mapping[str, Any],
        resource: Any,
        agent: Any,
        workspace: Any,
        runtime: Mapping[str, Any],
        on_error: str,
    ) -> InterventionHandler:
        definition = self._get("intervention", registration_id)
        if definition is None:
            raise AgentNotReadyError(
                f"intervention registration {registration_id!r} is unavailable"
            )
        context = _runtime_context(
            InterventionContext,
            definition,
            raw_config=config,
            resource=resource,
            agent=agent,
            workspace=workspace,
            runtime=runtime,
        )
        if definition.kind == "intervention":
            callbacks = [(definition.members[0].event_types[0], definition.target)]
        else:
            user_object = _construct_user_object(definition, context)
            callbacks = [
                (member.event_types[0], getattr(user_object, member.attribute))
                for member in definition.members
            ]
        return _intervention_adapter(
            definition,
            callbacks,
            context,
            runtime_name=f"{definition.name}:{resource.id}",
            on_error=on_error or definition.default_on_error,
        )

    def build_graph_condition(
        self,
        registration_id: str,
        *,
        edge: Any,
        orchestration: Any,
        workspace: Any,
    ) -> Callable[..., bool]:
        definition = self._get("graph_condition", registration_id)
        if definition is None:
            raise AgentNotReadyError(f"graph condition {registration_id!r} is unavailable")
        config = _validated_config(definition, getattr(edge, "condition_config", {}))
        context = GraphConditionContext(
            config=config,
            workspace_id=workspace.workspace.id,
            orchestration_id=orchestration.id,
            edge_id=edge.id,
            source_node_id=edge.source,
            target_node_id=edge.target,
        )
        parameters = inspect.signature(definition.target).parameters

        @wraps(definition.target)
        def condition(
            state: Any,
            *,
            invocation_state: dict[str, Any],
            **_kwargs: Any,
        ) -> bool:
            kwargs: dict[str, Any] = {}
            if "context" in parameters:
                kwargs["context"] = context
            if "invocation_state" in parameters:
                kwargs["invocation_state"] = invocation_state
            result = definition.target(state, **kwargs)
            if inspect.isawaitable(result):
                raise CodeRegistrationError("graph conditions must be synchronous")
            if not isinstance(result, bool):
                raise CodeRegistrationError("graph conditions must return bool")
            return result

        # ``wraps`` intentionally preserves the registered function's metadata, but it
        # also exposes that function through ``__wrapped__``.  Strands distinguishes its
        # legacy and context-aware edge conditions with ``inspect.signature``; without
        # an explicit adapter signature it therefore sees the user's ``(state,
        # context)`` signature and calls this wrapper without ``invocation_state``.
        # Pin the public signature to the adapter itself so Strands selects its
        # context-aware calling convention while introspection keeps the useful name
        # and documentation copied above.
        vars(condition)["__signature__"] = inspect.signature(condition, follow_wrapped=False)
        return condition


def extension_close_callback(value: Any) -> Callable[[], Any] | None:
    """Return one async cleanup callback for a MeldFabric adapter when available."""

    if isinstance(value, (_RegisteredPluginAdapter, _HookGroupAdapter)):
        return value.close
    user_object = getattr(value, "__meldfabric_user_object__", None)
    closer = getattr(user_object, "close", None)
    if closer is None:
        return None

    async def close() -> None:
        result = closer()
        if inspect.isawaitable(result):
            await result

    return close
