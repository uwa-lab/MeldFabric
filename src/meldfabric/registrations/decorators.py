"""Public decorator API for trusted, import-time code registration."""

from __future__ import annotations

import inspect
import re
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel
from strands.hooks import (
    AfterModelCallEvent,
    AfterToolCallEvent,
    AgentInitializedEvent,
    BaseHookEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    MultiAgentInitializedEvent,
)

from meldfabric.config.structured import validate_json_schema
from meldfabric.errors import CodeRegistrationError
from meldfabric.registrations.catalog import (
    MemberDefinition,
    RegistrationDefinition,
    RegistrationKind,
    catalog,
    stable_registration_id,
    validate_registration_name,
)
from meldfabric.registrations.types import EmptyConfig

DecoratorTarget = Callable[..., Any] | type[Any]
_INTERVENTION_EVENTS = {
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    AfterToolCallEvent,
    BeforeModelCallEvent,
    AfterModelCallEvent,
}


@dataclass(frozen=True, slots=True)
class _CallbackMarker:
    kind: Literal["hook", "intervention"]
    scope: Literal["hook_group", "plugin", "intervention_group"]
    event_types: tuple[type[BaseHookEvent], ...]
    priority: float = 0


@dataclass(frozen=True, slots=True)
class _PluginToolMarker:
    definition: Any


def _attach_metadata(target: DecoratorTarget, attribute: str, value: Any) -> None:
    """Attach registration metadata to either a function or a class declaration."""

    setattr(target, attribute, value)


def _default_name(target: DecoratorTarget) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", target.__name__).lower()
    for suffix in ("_plugin", "_hooks", "_hook", "_intervention", "_condition"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    return validate_registration_name(name)


def _validate_id(
    registration_id: str | None, kind: RegistrationKind, target: DecoratorTarget
) -> str:
    value = registration_id or stable_registration_id(kind, target)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
        raise CodeRegistrationError(
            "registration id must start with an ASCII letter and contain only letters, "
            "digits, '.', '_' or '-'",
            details={"registration_id": value},
        )
    return value


def _validate_config_model(value: type[BaseModel] | None) -> type[BaseModel]:
    if value is None:
        return EmptyConfig
    if not inspect.isclass(value) or not issubclass(value, BaseModel):
        raise CodeRegistrationError("config_model must be a pydantic BaseModel subclass")
    # Build the schema at import time so unsupported annotations fail before service startup.
    try:
        schema = value.model_json_schema()
        validate_json_schema(schema, source=f"config model {value.__name__}")
    except Exception as exc:
        raise CodeRegistrationError(
            f"could not build JSON Schema for config model {value.__name__}: {exc}"
        ) from exc
    return value


def _event_types(function: Callable[..., Any], *, method: bool) -> tuple[type[BaseHookEvent], ...]:
    parameters = list(inspect.signature(function).parameters.values())
    if method:
        if not parameters or parameters[0].name not in {"self", "cls"}:
            raise CodeRegistrationError(
                f"callback method {function.__qualname__} must declare self as its first parameter"
            )
        parameters = parameters[1:]
    if not parameters:
        raise CodeRegistrationError(
            f"callback {function.__qualname__} must accept a typed event parameter"
        )
    event_parameter = parameters[0]
    extras = parameters[1:]
    for parameter in extras:
        if parameter.name != "context" or parameter.kind not in {
            parameter.KEYWORD_ONLY,
            parameter.POSITIONAL_OR_KEYWORD,
        }:
            raise CodeRegistrationError(
                f"callback {function.__qualname__} may only accept event and an optional "
                "context parameter"
            )
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as exc:
        raise CodeRegistrationError(
            f"could not resolve callback annotations for {function.__qualname__}: {exc}"
        ) from exc
    annotation = hints.get(event_parameter.name, event_parameter.annotation)
    if annotation is inspect.Signature.empty:
        raise CodeRegistrationError(
            f"callback {function.__qualname__} event parameter needs a type annotation"
        )
    origin = get_origin(annotation)
    candidates = get_args(annotation) if origin in {Union, types.UnionType} else (annotation,)
    if not candidates or any(
        not inspect.isclass(candidate) or not issubclass(candidate, BaseHookEvent)
        for candidate in candidates
    ):
        raise CodeRegistrationError(
            f"callback {function.__qualname__} event annotation must be a Strands hook Event "
            "type or a union of Event types"
        )
    result = tuple(candidates)
    if inspect.iscoroutinefunction(function) and any(
        candidate in {AgentInitializedEvent, MultiAgentInitializedEvent} for candidate in result
    ):
        raise CodeRegistrationError(
            "AgentInitializedEvent and MultiAgentInitializedEvent callbacks must be synchronous"
        )
    return result


def _accepts_context(function: Callable[..., Any], *, method: bool = False) -> bool:
    parameters = list(inspect.signature(function).parameters.values())
    del method
    return any(parameter.name == "context" for parameter in parameters)


def _python_path(target: DecoratorTarget) -> str:
    return f"{target.__module__}:{target.__qualname__}"


def _validate_registered_class(target: type[Any]) -> None:
    parameters = list(inspect.signature(target).parameters.values())
    if len(parameters) > 1 or any(
        parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
        for parameter in parameters
    ):
        raise CodeRegistrationError(
            f"registered class {target.__qualname__} must accept no constructor arguments "
            "or one context argument"
        )
    if parameters and parameters[0].name != "context":
        raise CodeRegistrationError(
            f"registered class {target.__qualname__} constructor parameter must be named 'context'"
        )
    for method_name, allowed_count in (("init_agent", 2), ("close", 1)):
        method = getattr(target, method_name, None)
        if method is None:
            continue
        method_parameters = list(inspect.signature(method).parameters.values())
        if len(method_parameters) != allowed_count:
            raise CodeRegistrationError(
                f"{target.__qualname__}.{method_name} has an invalid signature"
            )
        expected_names = ["self", "agent"] if method_name == "init_agent" else ["self"]
        if [item.name for item in method_parameters] != expected_names:
            raise CodeRegistrationError(
                f"{target.__qualname__}.{method_name} parameters must be "
                f"{', '.join(expected_names)}"
            )


def _members(target: type[Any], allowed: set[str]) -> tuple[MemberDefinition, ...]:
    result: list[MemberDefinition] = []
    seen: set[str] = set()
    # Subclass declarations win over inherited members with the same attribute name.
    for owner in target.__mro__:
        for attribute, raw in owner.__dict__.items():
            if attribute in seen:
                continue
            seen.add(attribute)
            callback = getattr(raw, "__func__", raw)
            marker = getattr(callback, "__meldfabric_callback__", None)
            if isinstance(marker, _CallbackMarker) and marker.scope not in allowed:
                raise CodeRegistrationError(
                    f"callback marker on {target.__qualname__}.{attribute} is not valid "
                    "for this registration type"
                )
            if isinstance(marker, _CallbackMarker):
                result.append(
                    MemberDefinition(
                        kind=marker.kind,
                        attribute=attribute,
                        event_types=marker.event_types,
                        priority=marker.priority,
                    )
                )
            tool_marker = getattr(callback, "__meldfabric_plugin_tool__", None)
            if isinstance(tool_marker, _PluginToolMarker) and "tool" not in allowed:
                raise CodeRegistrationError(
                    f"@plugin_tool is only valid inside @register.plugin: "
                    f"{target.__qualname__}.{attribute}"
                )
            if "tool" in allowed and isinstance(tool_marker, _PluginToolMarker):
                result.append(
                    MemberDefinition(
                        kind="tool",
                        attribute=attribute,
                        payload=tool_marker.definition,
                    )
                )
    return tuple(result)


def hook_callback(
    function: Callable[..., Any] | None = None,
    *,
    priority: float = 0,
):
    """Mark one method inside ``@register.hook_group`` as a lifecycle callback."""

    def decorate(target: Callable[..., Any]):
        _attach_metadata(
            target,
            "__meldfabric_callback__",
            _CallbackMarker(
                "hook", "hook_group", _event_types(target, method=True), float(priority)
            ),
        )
        return target

    return decorate(function) if function is not None else decorate


def intervention_callback(function: Callable[..., Any] | None = None):
    """Mark one method inside an intervention group as a typed policy callback."""

    def decorate(target: Callable[..., Any]):
        event_types = _event_types(target, method=True)
        if len(event_types) != 1:
            raise CodeRegistrationError(
                "intervention callbacks must declare exactly one Event type"
            )
        if event_types[0] not in _INTERVENTION_EVENTS:
            raise CodeRegistrationError(
                f"{event_types[0].__name__} is not an Intervention lifecycle event"
            )
        _attach_metadata(
            target,
            "__meldfabric_callback__",
            _CallbackMarker("intervention", "intervention_group", event_types),
        )
        return target

    return decorate(function) if function is not None else decorate


def plugin_hook(
    function: Callable[..., Any] | None = None,
    *,
    priority: float = 0,
):
    """Mark one method inside a registered plugin as a lifecycle callback."""

    def decorate(target: Callable[..., Any]):
        _attach_metadata(
            target,
            "__meldfabric_callback__",
            _CallbackMarker("hook", "plugin", _event_types(target, method=True), float(priority)),
        )
        return target

    return decorate(function) if function is not None else decorate


def plugin_tool(
    function: Callable[..., Any] | None = None,
    *,
    id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    requires_sandbox: bool = False,
):
    """Mark one method as a tool owned by and mounted with its plugin."""

    def decorate(target: Callable[..., Any]):
        from meldfabric.tools.registry import build_tool_definition

        if requires_sandbox:
            raise CodeRegistrationError(
                "plugin tools cannot use requires_sandbox because their instance state cannot "
                "be reconstructed safely in an isolated process; register a standalone "
                "@register.tool instead"
            )
        definition = build_tool_definition(
            target,
            tool_id=id,
            name=name,
            description=description,
            requires_sandbox=requires_sandbox,
            skip_first_parameter=True,
        )
        _attach_metadata(target, "__meldfabric_plugin_tool__", _PluginToolMarker(definition))
        return target

    return decorate(function) if function is not None else decorate


class RegistrationAPI:
    """Namespace that keeps MeldFabric registration names separate from Strands decorators."""

    def tool(self, function=None, **kwargs):
        from meldfabric.tools.registry import tool as register_tool

        return register_tool(function, **kwargs)

    def hook(
        self,
        function: Callable[..., Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
        priority: float = 0,
    ):
        def decorate(target: Callable[..., Any]):
            event_types = _event_types(target, method=False)
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "hook", target),
                kind="hook",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
                members=(
                    MemberDefinition(
                        kind="hook",
                        attribute=target.__name__,
                        event_types=event_types,
                        priority=float(priority),
                    ),
                ),
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(function) if function is not None else decorate

    def hook_group(
        self,
        cls: type[Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
    ):
        def decorate(target: type[Any]):
            if not inspect.isclass(target):
                raise CodeRegistrationError("register.hook_group can only decorate a class")
            _validate_registered_class(target)
            members = _members(target, {"hook_group"})
            if not members:
                raise CodeRegistrationError(
                    f"hook group {target.__qualname__} has no @hook_callback methods"
                )
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "hook_group", target),
                kind="hook_group",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
                members=members,
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(cls) if cls is not None else decorate

    def intervention(
        self,
        function: Callable[..., Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
        on_error: Literal["throw", "proceed", "deny"] = "throw",
    ):
        def decorate(target: Callable[..., Any]):
            events = _event_types(target, method=False)
            if len(events) != 1:
                raise CodeRegistrationError(
                    "registered intervention functions must declare exactly one Event type"
                )
            if events[0] not in _INTERVENTION_EVENTS:
                raise CodeRegistrationError(
                    f"{events[0].__name__} is not an Intervention lifecycle event"
                )
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "intervention", target),
                kind="intervention",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
                members=(
                    MemberDefinition(
                        kind="intervention",
                        attribute=target.__name__,
                        event_types=events,
                    ),
                ),
                default_on_error=on_error,
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(function) if function is not None else decorate

    def intervention_group(
        self,
        cls: type[Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
        on_error: Literal["throw", "proceed", "deny"] = "throw",
    ):
        def decorate(target: type[Any]):
            if not inspect.isclass(target):
                raise CodeRegistrationError("register.intervention_group can only decorate a class")
            members = _members(target, {"intervention_group"})
            if not members:
                raise CodeRegistrationError(
                    f"intervention group {target.__qualname__} has no "
                    "@intervention_callback methods"
                )
            _validate_registered_class(target)
            events = [member.event_types[0] for member in members]
            if len(events) != len(set(events)):
                raise CodeRegistrationError(
                    "an intervention group may declare at most one callback for each Event type"
                )
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "intervention_group", target),
                kind="intervention_group",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
                members=members,
                default_on_error=on_error,
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(cls) if cls is not None else decorate

    def plugin(
        self,
        cls: type[Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
    ):
        def decorate(target: type[Any]):
            if not inspect.isclass(target):
                raise CodeRegistrationError("register.plugin can only decorate a class")
            _validate_registered_class(target)
            members = _members(target, {"plugin", "tool"})
            tool_names = [member.payload.name for member in members if member.kind == "tool"]
            if len(tool_names) != len(set(tool_names)):
                raise CodeRegistrationError(
                    f"plugin {target.__qualname__} declares duplicate tool names"
                )
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "plugin", target),
                kind="plugin",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
                members=members,
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(cls) if cls is not None else decorate

    def graph_condition(
        self,
        function: Callable[..., Any] | None = None,
        *,
        id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        config_model: type[BaseModel] | None = None,
    ):
        def decorate(target: Callable[..., Any]):
            if inspect.iscoroutinefunction(target):
                raise CodeRegistrationError("graph conditions must be synchronous")
            parameters = list(inspect.signature(target).parameters.values())
            if not parameters:
                raise CodeRegistrationError("graph conditions must accept the current GraphState")
            if parameters[0].name != "state" or parameters[0].kind not in {
                parameters[0].POSITIONAL_ONLY,
                parameters[0].POSITIONAL_OR_KEYWORD,
            }:
                raise CodeRegistrationError(
                    "graph conditions must declare state as the first positional parameter"
                )
            allowed = {"state", "context", "invocation_state"}
            unexpected = [item.name for item in parameters if item.name not in allowed]
            if unexpected:
                raise CodeRegistrationError(
                    "graph conditions may only accept state, context and invocation_state",
                    details={"unexpected_parameters": unexpected},
                )
            return_annotation = inspect.signature(target).return_annotation
            if return_annotation not in {inspect.Signature.empty, bool, "bool"}:
                raise CodeRegistrationError("graph condition return annotation must be bool")
            resolved_name = validate_registration_name(name or _default_name(target))
            definition = RegistrationDefinition(
                id=_validate_id(id, "graph_condition", target),
                kind="graph_condition",
                name=resolved_name,
                description=description or inspect.getdoc(target) or resolved_name,
                python_path=_python_path(target),
                module=target.__module__,
                target=target,
                config_model=_validate_config_model(config_model),
            )
            catalog.register(definition)
            _attach_metadata(target, "__meldfabric_registration__", definition)
            return target

        return decorate(function) if function is not None else decorate


register = RegistrationAPI()


def callback_accepts_context(function: Callable[..., Any], *, method: bool = False) -> bool:
    """Internal adapter helper kept here so declaration validation and invocation agree."""

    return _accepts_context(function, method=method)
