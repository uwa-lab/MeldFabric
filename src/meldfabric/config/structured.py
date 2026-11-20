"""Strict JSON values and schemas shared by the control plane and admin UI.

The control database only stores JSON documents.  Keeping this contract in one
module prevents a permissive ``dict[str, Any]`` from accepting Python objects
that later fail during persistence, API serialization, or visual editing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from meldfabric.errors import ConfigurationError


def validate_json_value(value: Any, *, path: str = "$") -> Any:
    """Reject values that cannot be represented by strict RFC 8259 JSON."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{path} must contain a finite JSON number")
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_value(item, path=f"{path}[{index}]")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string object key")
            validate_json_value(item, path=f"{path}.{key}")
        return value
    raise ValueError(f"{path} contains unsupported JSON value {type(value).__name__}")


def validate_json_object(value: dict[str, Any]) -> dict[str, Any]:
    validate_json_value(value)
    return value


def _schema_error_details(exc: Exception, *, source: str) -> dict[str, Any]:
    path = list(getattr(exc, "absolute_path", ()) or getattr(exc, "path", ()))
    schema_path = list(getattr(exc, "absolute_schema_path", ()))
    return {
        "source": source,
        "path": path,
        "schema_path": schema_path,
        "message": getattr(exc, "message", str(exc)),
    }


def _walk_schema(value: Any, *, path: tuple[str | int, ...] = ()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _walk_schema(child, path=(*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_schema(child, path=(*path, index))


def _resolve_local_reference(document: dict[str, Any], reference: str) -> Any:
    if reference == "#":
        return document
    if reference.startswith("#/"):
        target: Any = document
        for encoded in unquote(reference[2:]).split("/"):
            part = encoded.replace("~1", "/").replace("~0", "~")
            target = target[int(part)] if isinstance(target, list) else target[part]
        return target
    anchor = unquote(reference[1:])
    for _path, node in _walk_schema(document):
        if node.get("$anchor") == anchor or node.get("$dynamicAnchor") == anchor:
            return node
    raise KeyError(anchor)


def validate_json_schema(
    schema: Mapping[str, Any],
    *,
    source: str,
    require_object: bool = False,
) -> dict[str, Any]:
    """Validate a deterministic, local Draft 2020-12 JSON Schema document."""

    document = deepcopy(dict(schema))
    try:
        validate_json_value(document)
        json.dumps(document, allow_nan=False, sort_keys=True)
        Draft202012Validator.check_schema(document)
    except (TypeError, ValueError, SchemaError) as exc:
        raise ConfigurationError(
            f"{source} is not a valid JSON Schema",
            details=_schema_error_details(exc, source=source),
        ) from exc
    if require_object and document.get("type", "object") != "object":
        raise ConfigurationError(
            f"{source} must describe a JSON object",
            details={"source": source, "path": ["type"], "expected": "object"},
        )
    for path, node in _walk_schema(document):
        reference = node.get("$ref")
        if not isinstance(reference, str):
            continue
        if not reference.startswith("#"):
            reason = "remote JSON Schema references are not supported"
        else:
            try:
                _resolve_local_reference(document, reference)
            except (KeyError, IndexError, TypeError, ValueError):
                reason = "the local JSON Schema reference does not exist"
            else:
                continue
        raise ConfigurationError(
            f"{source} contains an invalid JSON Schema reference",
            details={
                "source": source,
                "path": [*path, "$ref"],
                "reference": reference,
                "reason": reason,
            },
        )
    return document


def validate_value_against_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    source: str,
) -> None:
    validate_json_value(value)
    document = validate_json_schema(schema, source=f"{source} schema")
    try:
        Draft202012Validator(document).validate(value)
    except JsonSchemaValidationError as exc:
        raise ConfigurationError(
            f"{source} does not match its JSON Schema",
            details=_schema_error_details(exc, source=source),
        ) from exc


DATABASE_OPTIONS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "数据库连接选项",
    "description": "传递给 SQLAlchemy 异步引擎的受控连接与连接池参数。",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pool_size": {
            "type": "integer",
            "minimum": 1,
            "default": 5,
            "title": "连接池大小",
            "description": "连接池长期保留的连接数量。",
        },
        "max_overflow": {
            "type": "integer",
            "minimum": -1,
            "default": 10,
            "title": "最大溢出连接",
            "description": "连接池满载后允许临时创建的连接数。-1 表示不限制。",
        },
        "pool_timeout": {
            "type": "number",
            "exclusiveMinimum": 0,
            "default": 30,
            "title": "连接等待超时",
            "description": "从连接池等待连接的最长秒数。",
        },
        "pool_recycle": {
            "type": "number",
            "minimum": -1,
            "default": -1,
            "title": "连接回收时间",
            "description": "连接存活多少秒后回收。-1 表示不主动回收。",
        },
        "pool_use_lifo": {
            "type": "boolean",
            "default": False,
            "title": "后进先出",
            "description": "优先复用最近归还的连接。",
        },
        "isolation_level": {
            "type": "string",
            "title": "事务隔离级别",
            "description": "留空时使用数据库驱动默认隔离级别。",
        },
        "query_cache_size": {
            "type": "integer",
            "minimum": 0,
            "default": 500,
            "title": "SQL 编译缓存",
            "description": "SQL 语句编译缓存的目标数量。",
        },
        "connect_args": {
            "type": "object",
            "title": "驱动连接参数",
            "description": "直接传给数据库驱动的 JSON 参数。",
            "additionalProperties": {},
            "default": {},
        },
        "execution_options": {
            "type": "object",
            "title": "执行选项",
            "description": "应用到引擎的 SQLAlchemy 执行选项。",
            "additionalProperties": {},
            "default": {},
        },
        "echo": {
            "oneOf": [
                {"type": "boolean", "title": "开关"},
                {"type": "string", "enum": ["debug"], "title": "调试模式"},
            ],
            "default": False,
            "title": "SQL 日志",
            "description": "输出 SQL 日志。生产环境通常保持关闭。",
        },
        "echo_pool": {
            "oneOf": [
                {"type": "boolean", "title": "开关"},
                {"type": "string", "enum": ["debug"], "title": "调试模式"},
            ],
            "default": False,
            "title": "连接池日志",
            "description": "输出连接池日志。生产环境通常保持关闭。",
        },
    },
}


GENERIC_JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": {},
}


def database_options_schema(_engine: str | None = None) -> dict[str, Any]:
    # SQLAlchemy engines share a deliberately small, controlled option surface.
    # Custom adapters own their option contract and therefore receive a generic
    # JSON object until the adapter publishes a more specific schema.
    if _engine == "custom":
        return {
            **deepcopy(GENERIC_JSON_OBJECT_SCHEMA),
            "title": "自定义数据库连接选项",
            "description": "由自定义数据库适配器解释的 JSON 参数。",
        }
    return deepcopy(DATABASE_OPTIONS_SCHEMA)


def validate_database_options(options: Mapping[str, Any], *, engine: str) -> dict[str, Any]:
    value = dict(options)
    validate_value_against_schema(
        value,
        database_options_schema(engine),
        source=f"{engine} database options",
    )
    return value


def structured_schema_catalog(
    *,
    sandbox_schemas: Mapping[str, Mapping[str, Any]],
    model_parameter_schemas: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "dialect": "https://json-schema.org/draft/2020-12/schema",
        "database_options": {
            engine: database_options_schema(engine)
            for engine in ("sqlite", "postgresql", "mysql", "custom")
        },
        "sandbox_config": {
            name: validate_json_schema(schema, source=f"sandbox provider {name!r} config")
            for name, schema in sandbox_schemas.items()
        },
        "model_parameters": {
            name: validate_json_schema(schema, source=f"model adapter {name!r} parameters")
            for name, schema in model_parameter_schemas.items()
        },
        "json_object": deepcopy(GENERIC_JSON_OBJECT_SCHEMA),
    }


def validate_workspace_structured_data(
    config: Any,
    *,
    registration_get: Any,
    sandbox_schemas: Mapping[str, Mapping[str, Any]],
    model_parameter_schemas: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate every schema-backed value before startup or persistence."""

    resources = config.resources
    providers = {item.id: item for item in resources.model_providers}
    for model in resources.models:
        provider = providers.get(model.provider_ref)
        schema = model_parameter_schemas.get(provider.adapter) if provider is not None else None
        if schema is not None:
            validate_value_against_schema(
                model.parameters,
                schema,
                source=f"model {model.name!r} parameters",
            )
    for profile in resources.sandbox_profiles:
        schema = sandbox_schemas.get(profile.provider)
        if schema is not None:
            validate_value_against_schema(
                profile.config,
                schema,
                source=f"sandbox profile {profile.name!r} config",
            )

    registered = [
        ("plugin", item.registration_id, item.name, item.config)
        for item in resources.plugins
        if getattr(item, "kind", None) == "registered"
    ]
    registered.extend(
        ("hook", item.registration_id, item.name, item.config) for item in resources.hooks
    )
    registered.extend(
        ("intervention", item.registration_id, item.name, item.config)
        for item in resources.interventions
        if getattr(item, "kind", None) == "registered"
    )
    for kind, registration, name, value in registered:
        definition = registration_get(kind, registration)
        if definition is None:
            continue
        schema = definition.config_schema or GENERIC_JSON_OBJECT_SCHEMA
        validate_value_against_schema(
            value,
            schema,
            source=f"{kind} resource {name!r} config",
        )
        try:
            definition.config_model.model_validate(value)
        except Exception as exc:
            raise ConfigurationError(
                f"{kind} resource {name!r} config is invalid",
                details={
                    "kind": kind,
                    "registration": registration,
                    "resource": name,
                    "message": str(exc),
                },
            ) from exc

    for orchestration in config.orchestrations:
        for edge in getattr(orchestration, "edges", ()):
            registration = getattr(edge, "condition_registration_id", None)
            if not registration:
                continue
            definition = registration_get("graph_condition", registration)
            if definition is None:
                continue
            validate_value_against_schema(
                edge.condition_config,
                definition.config_schema or GENERIC_JSON_OBJECT_SCHEMA,
                source=f"graph edge {edge.id!r} condition config",
            )
            try:
                definition.config_model.model_validate(edge.condition_config)
            except Exception as exc:
                raise ConfigurationError(
                    f"graph edge {edge.id!r} condition config is invalid",
                    details={
                        "registration": registration,
                        "edge_id": edge.id,
                        "message": str(exc),
                    },
                ) from exc

    for tool in resources.tools:
        definition = registration_get("tool", tool.id)
        if definition is None or definition.payload is None:
            continue
        properties = definition.payload.input_schema.get("properties") or {}
        unknown = sorted(set(tool.parameter_descriptions) - set(properties))
        if unknown:
            raise ConfigurationError(
                f"tool {tool.name!r} has descriptions for unknown parameters",
                details={"tool_id": tool.id, "parameters": unknown},
            )
