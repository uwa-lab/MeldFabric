"""Provider adapter registry for model discovery and construction."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

import httpx

from meldfabric.config.models import ModelCapabilities, ModelProvider, ModelResource
from meldfabric.config.structured import GENERIC_JSON_OBJECT_SCHEMA, validate_json_schema
from meldfabric.errors import (
    AgentNotReadyError,
    ModelCredentialMissingError,
    OptionalDependencyError,
)

if TYPE_CHECKING:
    from meldfabric.storage import SecretReader


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    id: str
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        # Only explicit boolean fields have a stable meaning. Free-text tags and
        # provider-specific endpoint names are displayed, never used as rules.
        capabilities: dict[str, bool] = {}
        for source in (
            self.metadata,
            self.metadata.get("profile"),
            self.metadata.get("capabilities"),
        ):
            if isinstance(source, dict):
                capabilities.update(
                    {
                        key: value
                        for key, value in source.items()
                        if key in ModelCapabilities.model_fields and type(value) is bool
                    }
                )
        return {"id": self.id, "metadata": self.metadata, "capabilities": capabilities}


class ModelAdapter(Protocol):
    name: ClassVar[str]
    requires_api_key: ClassVar[bool]
    parameters_schema: ClassVar[dict[str, Any]]

    def build_strands(
        self,
        provider: ModelProvider,
        model: ModelResource,
        *,
        api_key: str | None,
    ) -> Any: ...

    async def discover(
        self,
        provider: ModelProvider,
        *,
        api_key: str | None,
    ) -> list[DiscoveredModel]: ...


class OpenAICompatibleAdapter:
    name: ClassVar[str] = "openai_compatible"
    requires_api_key: ClassVar[bool] = True
    parameters_schema: ClassVar[dict[str, Any]] = {
        **GENERIC_JSON_OBJECT_SCHEMA,
        "title": "模型请求参数",
        "description": "传递给 OpenAI Compatible 模型的额外推理参数。",
        "properties": {
            "max_tokens": {
                "type": "integer",
                "minimum": 1,
                "title": "最大输出 Token",
            },
            "stop": {
                "oneOf": [
                    {"type": "string", "title": "单个停止序列"},
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "title": "多个停止序列",
                    },
                ],
                "title": "停止序列",
            },
            "presence_penalty": {
                "type": "number",
                "minimum": -2,
                "maximum": 2,
                "title": "存在惩罚",
            },
            "frequency_penalty": {
                "type": "number",
                "minimum": -2,
                "maximum": 2,
                "title": "频率惩罚",
            },
            "seed": {"type": "integer", "title": "随机种子"},
        },
    }

    def build_strands(
        self,
        provider: ModelProvider,
        model: ModelResource,
        *,
        api_key: str | None,
    ) -> Any:
        """Construct the native Strands OpenAI-compatible provider."""

        try:
            from strands.models.openai import OpenAIModel
            from strands.models.openai_responses import OpenAIResponsesModel
            from strands.types.content import ContentBlock
        except ImportError as exc:
            raise OptionalDependencyError(
                "Core Strands OpenAI dependencies are missing; reinstall meldfabric"
            ) from exc

        class MeldFabricOpenAIModel(OpenAIModel):
            """Add the OpenAI chat-completions audio block missing upstream."""

            @classmethod
            def format_request_message_content(
                cls,
                content: ContentBlock,
                **kwargs: Any,
            ) -> dict[str, Any]:
                if "audio" not in content:
                    return super().format_request_message_content(content, **kwargs)
                audio = content["audio"]
                source = audio.get("source", {})
                payload = source.get("bytes")
                if not isinstance(payload, bytes):
                    raise TypeError("OpenAI-compatible audio input requires inline bytes")
                return {
                    "type": "input_audio",
                    "input_audio": {
                        "data": base64.b64encode(payload).decode("ascii"),
                        "format": audio.get("format", "wav"),
                    },
                }

        params = dict(model.parameters)
        for key, value in {
            "temperature": model.temperature,
            "top_p": model.top_p,
        }.items():
            if value is not None:
                params[key] = value

        client_args: dict[str, Any] = {
            "api_key": api_key,
            "base_url": provider.base_url,
            "default_headers": dict(provider.headers),
            "timeout": model.timeout_seconds or provider.timeout_seconds,
        }
        if model.max_retries is not None:
            client_args["max_retries"] = model.max_retries
        model_class = (
            OpenAIResponsesModel if model.protocol == "responses" else MeldFabricOpenAIModel
        )
        return model_class(
            client_args=client_args,
            model_id=model.model_name,
            params=params,
        )

    async def discover(
        self,
        provider: ModelProvider,
        *,
        api_key: str | None,
    ) -> list[DiscoveredModel]:
        headers = dict(provider.headers)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
            response = await client.get(
                f"{provider.base_url.rstrip('/')}/models",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return [
            DiscoveredModel(id=str(item["id"]), metadata=dict(item))
            for item in items
            if isinstance(item, dict) and "id" in item
        ]


class ModelAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ModelAdapter] = {}
        self._parameter_schemas: dict[str, dict[str, Any]] = {}
        self.register(OpenAICompatibleAdapter())

    def register(self, adapter: ModelAdapter) -> None:
        schema = getattr(adapter, "parameters_schema", GENERIC_JSON_OBJECT_SCHEMA)
        validate_json_schema(
            schema,
            source=f"model adapter {adapter.name!r} parameters",
        )
        self._adapters[adapter.name] = adapter
        self._parameter_schemas[adapter.name] = dict(schema)

    def parameter_schemas(self) -> dict[str, dict[str, Any]]:
        return {name: dict(schema) for name, schema in sorted(self._parameter_schemas.items())}

    def get(self, name: str) -> ModelAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise AgentNotReadyError(
                f"model provider adapter {name!r} is not installed",
                details={"adapter": name},
            ) from exc

    async def resolve_api_key(
        self,
        provider: ModelProvider,
        *,
        secrets: SecretReader | None,
    ) -> str | None:
        adapter = self.get(provider.adapter)
        if provider.api_key_secret_ref is None:
            if adapter.requires_api_key:
                raise ModelCredentialMissingError(
                    f"model provider {provider.id!r} has no API key configured",
                    details={"provider_id": provider.id},
                )
            return None
        if secrets is None:
            raise ModelCredentialMissingError(
                "the control secret store is not ready, so the model credential cannot be loaded",
                details={"provider_id": provider.id},
            )
        value = await secrets.get_secret(provider.api_key_secret_ref)
        if value is None:
            raise ModelCredentialMissingError(
                f"model provider {provider.id!r} credential was not found",
                details={
                    "provider_id": provider.id,
                    "secret_ref": provider.api_key_secret_ref,
                },
            )
        return value

    async def build_strands(
        self,
        provider: ModelProvider,
        model: ModelResource,
        *,
        secrets: SecretReader | None,
    ) -> Any:
        adapter = self.get(provider.adapter)
        api_key = await self.resolve_api_key(provider, secrets=secrets)
        builder = getattr(adapter, "build_strands", None)
        if builder is None:
            raise AgentNotReadyError(
                f"model provider adapter {provider.adapter!r} has no Strands implementation",
                details={"adapter": provider.adapter, "provider_id": provider.id},
            )
        return await asyncio.to_thread(
            builder,
            provider,
            model,
            api_key=api_key,
        )

    async def discover(
        self,
        provider: ModelProvider,
        *,
        secrets: SecretReader | None,
    ) -> list[DiscoveredModel]:
        adapter = self.get(provider.adapter)
        api_key = await self.resolve_api_key(provider, secrets=secrets)
        return await adapter.discover(provider, api_key=api_key)
