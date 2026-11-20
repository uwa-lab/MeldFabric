"""Import-time tool candidate registration with strict schema validation."""

from __future__ import annotations

import inspect
import json
import re
import types
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, create_model
from strands.types.tools import ToolContext

from meldfabric.config.structured import validate_json_schema
from meldfabric.domain.names import model_name
from meldfabric.errors import CodeRegistrationError, ToolRegistrationError, ToolResultError
from meldfabric.registrations.catalog import (
    RegistrationDefinition,
    stable_registration_id,
    validate_registration_name,
)
from meldfabric.registrations.catalog import (
    catalog as code_catalog,
)

F = TypeVar("F", bound=Callable[..., Any])


class ToolResult(BaseModel):
    """Normalized rich result supported by MeldFabric tools."""

    content: str | dict[str, Any] | list[Any] | None = None
    artifact: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    id: str
    name: str
    display_name: str | None
    description: str
    python_path: str
    input_schema: dict[str, Any]
    return_schema: dict[str, Any]
    input_model: type[BaseModel] = field(repr=False, compare=False)
    return_adapter: TypeAdapter[Any] = field(repr=False, compare=False)
    requires_sandbox: bool
    function: Callable[..., Any] = field(repr=False, compare=False)


def _stable_tool_id(function: Callable[..., Any]) -> str:
    return stable_registration_id("tool", function)


def _unwrap_annotation(annotation: Any) -> Any:
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def _is_supported_json_type(annotation: Any, *, allow_none: bool = True) -> bool:
    annotation = _unwrap_annotation(annotation)
    if annotation is Any:
        return False
    if annotation in {list, dict}:
        return True
    if annotation in {str, int, float, bool}:
        return True
    if annotation in {None, type(None)}:
        return allow_none
    if inspect.isclass(annotation) and issubclass(annotation, (BaseModel, Enum)):
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {list, tuple}:
        return bool(args) and all(
            item is Ellipsis or item is Any or _is_supported_json_type(item, allow_none=allow_none)
            for item in args
        )
    if origin is dict:
        return (
            len(args) == 2
            and args[0] is str
            and (args[1] is Any or _is_supported_json_type(args[1], allow_none=allow_none))
        )
    if origin is Literal:
        return bool(args) and all(
            item is None or isinstance(item, (str, int, float, bool)) for item in args
        )
    if origin in {Union, types.UnionType}:
        return bool(args) and all(
            _is_supported_json_type(item, allow_none=allow_none) for item in args
        )
    return False


def build_tool_definition(
    function: Callable[..., Any],
    *,
    tool_id: str | None,
    name: str | None,
    description: str | None,
    requires_sandbox: bool,
    skip_first_parameter: bool = False,
) -> ToolDefinition:
    signature = inspect.signature(function)
    try:
        hints = get_type_hints(function, include_extras=True)
    except (NameError, TypeError) as exc:
        raise ToolRegistrationError(
            f"could not resolve type annotations for tool {function.__qualname__}: {exc}"
        ) from exc

    fields: dict[str, Any] = {}
    parameters = list(signature.parameters.values())
    if skip_first_parameter:
        if not parameters:
            raise ToolRegistrationError(f"plugin tool {function.__qualname__} must declare self")
        parameters = parameters[1:]
    for parameter in parameters:
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            raise ToolRegistrationError(
                f"tool {function.__qualname__} cannot use *args or **kwargs"
            )
        annotation = hints.get(parameter.name, parameter.annotation)
        if annotation is inspect.Signature.empty:
            raise ToolRegistrationError(
                f"tool {function.__qualname__} parameter {parameter.name!r} needs a type annotation"
            )
        if not _is_supported_json_type(annotation):
            raise ToolRegistrationError(
                f"tool {function.__qualname__} parameter {parameter.name!r} has unsupported "
                f"type {annotation!r}"
            )
        default = parameter.default
        if default is inspect.Signature.empty:
            default = ...
        fields[parameter.name] = (annotation, default)

    return_annotation = hints.get("return", signature.return_annotation)
    if return_annotation is inspect.Signature.empty:
        raise ToolRegistrationError(
            f"tool {function.__qualname__} needs an explicit return type annotation"
        )
    if return_annotation is not ToolResult and not _is_supported_json_type(return_annotation):
        raise ToolRegistrationError(
            f"tool {function.__qualname__} has unsupported return type {return_annotation!r}"
        )

    input_model = create_model(f"{function.__name__.title()}ToolInput", **fields)
    return_schema = TypeAdapter(return_annotation).json_schema()
    return_adapter = TypeAdapter(return_annotation)
    resolved_name = model_name(name or function.__name__)
    resolved_id = tool_id or _stable_tool_id(function)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", resolved_id):
        raise ToolRegistrationError(
            "tool id must start with an ASCII letter and contain only letters, digits, "
            "'.', '_' or '-'; omit id to generate a stable identifier automatically"
        )
    python_path = f"{function.__module__}:{function.__qualname__}"
    input_schema = input_model.model_json_schema()
    try:
        validate_json_schema(input_schema, source=f"tool {resolved_name!r} input")
        validate_json_schema(return_schema, source=f"tool {resolved_name!r} return")
    except Exception as exc:
        raise ToolRegistrationError(
            f"tool {function.__qualname__} does not expose a valid JSON Schema"
        ) from exc
    return ToolDefinition(
        id=resolved_id,
        name=resolved_name,
        display_name=(name or function.__name__)
        if resolved_name != (name or function.__name__)
        else None,
        description=description or inspect.getdoc(function) or resolved_name,
        python_path=python_path,
        input_schema=input_schema,
        return_schema=return_schema,
        input_model=input_model,
        return_adapter=return_adapter,
        requires_sandbox=requires_sandbox,
        function=function,
    )


def tool(
    function: F | None = None,
    *,
    id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    requires_sandbox: bool = False,
) -> F | Callable[[F], F]:
    """Register a function as a code-owned MeldFabric tool."""

    def decorator(target: F) -> F:
        definition = build_tool_definition(
            target,
            tool_id=id,
            name=name,
            description=description,
            requires_sandbox=requires_sandbox,
        )
        try:
            code_catalog.register(
                RegistrationDefinition(
                    id=definition.id,
                    kind="tool",
                    name=validate_registration_name(definition.name),
                    description=definition.description,
                    python_path=definition.python_path,
                    module=target.__module__,
                    target=target,
                    payload=definition,
                )
            )
        except CodeRegistrationError as exc:
            raise ToolRegistrationError(exc.message, details=exc.details) from exc

        vars(target)["__meldfabric_agent_tool__"] = definition
        return target

    if function is not None:
        return decorator(function)
    return decorator


def get_registered_tools() -> tuple[ToolDefinition, ...]:
    return registered_tool_snapshot()[1]


def registered_tool_snapshot() -> tuple[int, tuple[ToolDefinition, ...]]:
    """Atomically read code registrations independently of configuration revisions."""
    generation, registrations = code_catalog.snapshot("tool")
    return generation, tuple(item.payload for item in registrations)


def _validate_tool_result(definition: ToolDefinition, value: Any) -> Any:
    try:
        validated = definition.return_adapter.validate_python(value)
    except ValidationError as exc:
        # Return validation is a tool implementation error, not invalid tool input.
        raise ToolResultError(
            f"tool {definition.id!r} returned a value incompatible with its declared return type",
            details={
                "tool_id": definition.id,
                "errors": exc.errors(include_input=False, include_url=False, include_context=False),
            },
        ) from exc
    try:
        serializable = definition.return_adapter.dump_python(validated, mode="json")
        json.dumps(serializable, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolResultError(
            f"tool {definition.id!r} returned a non-JSON-serializable value",
            details={"tool_id": definition.id},
        ) from exc
    return serializable


def _tool_input_schema(
    definition: ToolDefinition,
    parameter_descriptions: dict[str, str] | None,
) -> dict[str, Any]:
    """Return an isolated JSON schema with administrative descriptions applied."""

    schema = deepcopy(definition.input_schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, description in (parameter_descriptions or {}).items():
            field_schema = properties.get(name)
            if isinstance(field_schema, dict):
                field_schema["description"] = description
    return schema


def _strands_compatible_signature(function: Callable[..., Any]) -> inspect.Signature:
    """Expose a signature Strands can validate while keeping MeldFabric's richer schema.

    MeldFabric accepts Pydantic ``Field`` metadata inside ``Annotated`` so constraints and
    descriptions can drive the control-plane editor. Strands 1.64 intentionally
    rejects that metadata while constructing its own input model, even when an
    explicit ``inputSchema`` is supplied. The explicit MeldFabric schema remains authoritative;
    the runtime signature therefore only needs the resolved base Python types.
    """

    original = inspect.signature(function)
    hints = get_type_hints(function, include_extras=True)
    parameters = [
        parameter.replace(
            annotation=_unwrap_annotation(hints.get(parameter.name, parameter.annotation))
        )
        for parameter in original.parameters.values()
    ]
    return original.replace(
        parameters=parameters,
        return_annotation=_unwrap_annotation(hints.get("return", original.return_annotation)),
    )


def _apply_strands_signature(
    wrapper: Callable[..., Any],
    source: Callable[..., Any],
) -> inspect.Signature:
    signature = _strands_compatible_signature(source)
    vars(wrapper)["__signature__"] = signature
    wrapper.__annotations__ = {
        parameter.name: parameter.annotation
        for parameter in signature.parameters.values()
        if parameter.annotation is not inspect.Signature.empty
    }
    if signature.return_annotation is not inspect.Signature.empty:
        wrapper.__annotations__["return"] = signature.return_annotation
    return signature


def _validated_tool_kwargs(
    definition: ToolDefinition,
    values: dict[str, Any],
    *,
    json_mode: bool = False,
) -> dict[str, Any]:
    validated = definition.input_model.model_validate(values)
    if json_mode:
        return validated.model_dump(mode="json")
    return {name: getattr(validated, name) for name in definition.input_model.model_fields}


def build_strands_tool(
    definition: ToolDefinition,
    *,
    name: str,
    description: str,
    parameter_descriptions: dict[str, str] | None = None,
):
    """Build a native Strands tool from a MeldFabric import-time registration."""

    from functools import wraps

    from strands import tool as strands_tool

    schema = _tool_input_schema(definition, parameter_descriptions)

    if inspect.iscoroutinefunction(definition.function):

        @wraps(definition.function)
        async def invoke_async(**tool_kwargs: Any) -> Any:
            validated_kwargs = _validated_tool_kwargs(definition, tool_kwargs)
            return _validate_tool_result(
                definition,
                await definition.function(**validated_kwargs),
            )

        function = invoke_async
    else:

        @wraps(definition.function)
        def invoke_sync(**tool_kwargs: Any) -> Any:
            validated_kwargs = _validated_tool_kwargs(definition, tool_kwargs)
            return _validate_tool_result(
                definition,
                definition.function(**validated_kwargs),
            )

        function = invoke_sync

    _apply_strands_signature(function, definition.function)

    return strands_tool(
        name=name,
        description=description,
        inputSchema=schema,
    )(function)


def build_sandboxed_strands_tool(
    definition: ToolDefinition,
    *,
    name: str,
    description: str,
    executor: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]],
    parameter_descriptions: dict[str, str] | None = None,
):
    """Build a Strands tool that always delegates execution to a MeldFabric sandbox."""

    from strands import tool as strands_tool

    schema = _tool_input_schema(definition, parameter_descriptions)

    async def invoke_sandboxed(tool_context: ToolContext, **tool_kwargs: Any) -> Any:
        invocation_state = dict(tool_context.invocation_state)
        run_context = {
            "configurable": invocation_state,
            "metadata": dict(invocation_state.get("metadata") or {}),
        }
        return _validate_tool_result(
            definition,
            await executor(
                _validated_tool_kwargs(definition, tool_kwargs, json_mode=True),
                run_context,
            ),
        )

    # Strands validates tool input from the decorated callable's signature even
    # when an explicit JSON schema is supplied.  The runtime wrapper accepts
    # ``**tool_kwargs`` so it can serve every registered function, but its public
    # signature must remain the original typed function plus the injected context.
    original_signature = _apply_strands_signature(invoke_sandboxed, definition.function)
    context_parameter = inspect.Parameter(
        "tool_context",
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=ToolContext,
    )
    vars(invoke_sandboxed)["__signature__"] = original_signature.replace(
        parameters=[*original_signature.parameters.values(), context_parameter]
    )
    invoke_sandboxed.__annotations__ = {
        **get_type_hints(definition.function, include_extras=True),
        "tool_context": ToolContext,
    }

    return strands_tool(
        name=name,
        description=description,
        inputSchema=schema,
        context="tool_context",
    )(invoke_sandboxed)
